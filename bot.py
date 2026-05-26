"""
Polymarket Paper Trading Bot
Watches 5-minute BTC up/down markets on Polymarket,
calculates true probability vs market odds using live BTC price,
and paper trades when it finds an edge. Reports to Telegram.
"""

import asyncio
import aiohttp
import json
import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional
import anthropic
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# YOUR KEYS
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_KEY")

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────
STARTING_BALANCE   = 50.0
BET_SIZE_PCT       = 0.05    # 5% of balance per trade
MIN_EDGE_PCT       = 5.0     # Only bet when edge is 5%+
SCAN_INTERVAL      = 30      # Check every 30 seconds
MIN_TIME_REMAINING = 60      # Don't enter with less than 60s left
MAX_TIME_REMAINING = 240     # Don't enter more than 4 mins before end

# ─────────────────────────────────────────
# PAPER TRADING STATE
# ─────────────────────────────────────────
class PaperTrader:
    def __init__(self):
        self.balance       = STARTING_BALANCE
        self.start_balance = STARTING_BALANCE
        self.trades        = []
        self.open_bets     = []
        self.wins          = 0
        self.losses        = 0
        self.total_wagered = 0.0

    @property
    def pnl(self):
        return self.balance - self.start_balance

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    def place_bet(self, market_id, direction, odds, true_prob, edge, stake):
        bet = {
            "id":            len(self.trades) + 1,
            "market_id":     market_id,
            "direction":     direction,
            "odds":          odds,
            "true_prob":     true_prob,
            "edge":          edge,
            "stake":         stake,
            "potential_win": stake * (1 / odds) if odds < 1 else stake * odds,
            "time":          datetime.now(timezone.utc).isoformat(),
            "status":        "open"
        }
        self.balance -= stake
        self.total_wagered += stake
        self.open_bets.append(bet)
        self.trades.append(bet)
        return bet

    def settle_bet(self, bet_id, won: bool):
        for bet in self.open_bets:
            if bet["id"] == bet_id:
                bet["status"] = "won" if won else "lost"
                if won:
                    winnings = bet["stake"] / bet["odds"]
                    self.balance += bet["stake"] + winnings
                    self.wins += 1
                    profit = winnings
                else:
                    self.losses += 1
                    profit = -bet["stake"]
                bet["profit"] = profit
                self.open_bets.remove(bet)
                return bet, profit
        return None, 0

# ─────────────────────────────────────────
# POLYMARKET API
# ─────────────────────────────────────────
async def get_btc_markets(session: aiohttp.ClientSession) -> list:
    url = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": 50, "tag_slug": "crypto"}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            btc_markets = []
            for m in markets:
                q = (m.get("question", "") + m.get("description", "")).lower()
                if ("bitcoin" in q or "btc" in q) and ("5" in q or "five" in q) and ("up" in q or "down" in q or "higher" in q or "lower" in q):
                    btc_markets.append(m)
            log.info(f"Found {len(btc_markets)} BTC markets")
            return btc_markets
    except Exception as e:
        log.warning(f"Polymarket fetch error: {e}")
        return []

async def get_market_orderbook(session: aiohttp.ClientSession, condition_id: str) -> dict:
    url = "https://clob.polymarket.com/book"
    params = {"token_id": condition_id}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            return await r.json()
    except Exception as e:
        log.warning(f"Orderbook fetch error: {e}")
        return {}

# ─────────────────────────────────────────
# BTC PRICE FEED
# ─────────────────────────────────────────
async def get_btc_price(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            data = await r.json()
            return float(data["price"])
    except Exception:
        try:
            async with session.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                data = await r.json()
                return float(data["data"]["amount"])
        except Exception as e:
            log.warning(f"BTC price fetch error: {e}")
            return None

async def get_btc_volatility(session: aiohttp.ClientSession) -> float:
    try:
        async with session.get(
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            candles = await r.json()
            closes  = [float(c[4]) for c in candles]
            if len(closes) < 2:
                return 0.5
            returns  = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]
            mean     = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            return variance ** 0.5
    except Exception:
        return 0.5

# ─────────────────────────────────────────
# CLAUDE AI — edge calculator
# ─────────────────────────────────────────
def calculate_edge_with_claude(btc_price, btc_start_price, volatility,
                                market_up_odds, market_down_odds,
                                seconds_remaining, question) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    price_change_pct = ((btc_price - btc_start_price) / btc_start_price * 100)

    prompt = f"""You are a quantitative trader specialising in Polymarket prediction markets.

CURRENT SITUATION:
Market: {question}
BTC Current Price: ${btc_price:,.2f}
BTC Window Start Price: ${btc_start_price:,.2f}
Price Change So Far: {price_change_pct:+.3f}%
BTC 1-min Volatility: {volatility:.4f}% per minute
Seconds Remaining: {seconds_remaining}s

MARKET ODDS:
UP probability: {market_up_odds:.3f} ({market_up_odds*100:.1f}%)
DOWN probability: {market_down_odds:.3f} ({market_down_odds*100:.1f}%)

Calculate the TRUE probability of BTC ending UP vs DOWN.
Consider momentum, time remaining, and volatility.
Find edge = true probability minus market probability.
Only recommend betting if edge is meaningful.

Return ONLY this JSON, no markdown:
{{
  "true_prob_up": <float 0-1>,
  "true_prob_down": <float 0-1>,
  "edge_up": <true_prob_up minus market_prob_up>,
  "edge_down": <true_prob_down minus market_prob_down>,
  "best_bet": "UP" or "DOWN" or "NO_BET",
  "best_edge": <largest absolute edge as positive float>,
  "reasoning": "2-3 sentence explanation",
  "confidence": "HIGH" or "MEDIUM" or "LOW"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ─────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────
async def send_trade_alert(bot, trade, analysis, trader):
    direction_emoji = "📈" if trade["direction"] == "UP" else "📉"
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{direction_emoji} *PAPER TRADE PLACED*

*Direction:* {trade['direction']}
*Market Odds:* {trade['odds']*100:.1f}%
*True Probability:* {trade['true_prob']*100:.1f}%
*Edge:* +{trade['edge']:.1f}%
*Stake:* ${trade['stake']:.2f}
*Potential Win:* ${trade['potential_win']:.2f}

_{analysis.get('reasoning', '')}._

*Balance:* ${trader.balance:.2f} | *P&L:* {pnl_str}"""

    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_settlement_alert(bot, bet, profit, trader):
    emoji   = "✅" if profit > 0 else "❌"
    result  = "WON" if profit > 0 else "LOST"
    profit_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
    pnl_str    = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{emoji} *TRADE SETTLED — {result}*

*Direction:* {bet['direction']}
*Profit:* {profit_str}
*Stake was:* ${bet['stake']:.2f}

*Balance:* ${trader.balance:.2f}
*P&L:* {pnl_str}
*Win Rate:* {trader.win_rate:.0f}%
*Record:* {trader.wins}W / {trader.losses}L"""

    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_status(bot, trader, btc_price):
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    pnl_pct = trader.pnl / trader.start_balance * 100
    msg = f"""📊 *Status Update*

*BTC:* ${btc_price:,.2f}
*Balance:* ${trader.balance:.2f}
*P&L:* {pnl_str} ({pnl_pct:+.1f}%)
*Win Rate:* {trader.win_rate:.0f}%
*Trades:* {trader.wins + trader.losses} ({trader.wins}W / {trader.losses}L)
*Open Bets:* {len(trader.open_bets)}
*Time:* {datetime.now(timezone.utc).strftime('%H:%M UTC')}"""

    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────
class PolyBot:
    def __init__(self):
        self.bot             = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader          = PaperTrader()
        self.tracked_markets = {}
        self.last_status     = 0

    async def track_market(self, session, market):
        market_id = market.get("conditionId") or market.get("id", "")
        question  = market.get("question", "")
        if market_id in self.tracked_markets:
            return
        end_time = market.get("endDate") or market.get("end_date_iso", "")
        if not end_time:
            return
        try:
            end_dt         = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            now            = datetime.now(timezone.utc)
            secs_remaining = (end_dt - now).total_seconds()
            if secs_remaining < MIN_TIME_REMAINING or secs_remaining > MAX_TIME_REMAINING:
                return
            btc_price = await get_btc_price(session)
            if not btc_price:
                return
            self.tracked_markets[market_id] = {
                "start_price": btc_price,
                "end_time":    end_dt,
                "question":    question,
                "bet_placed":  False
            }
            log.info(f"Tracking: {question[:50]} | {secs_remaining:.0f}s | BTC ${btc_price:,.2f}")
        except Exception as e:
            log.warning(f"Track error: {e}")

    async def evaluate_market(self, session, market_id, market_data):
        if market_data.get("bet_placed"):
            return
        now            = datetime.now(timezone.utc)
        secs_remaining = (market_data["end_time"] - now).total_seconds()
        if secs_remaining < MIN_TIME_REMAINING or secs_remaining > MAX_TIME_REMAINING:
            return

        btc_price  = await get_btc_price(session)
        volatility = await get_btc_volatility(session)
        if not btc_price:
            return

        market_up_odds   = 0.50
        market_down_odds = 0.50
        try:
            book = await get_market_orderbook(session, market_id)
            if book and book.get("bids"):
                market_up_odds   = float(book["bids"][0]["price"])
                market_down_odds = 1 - market_up_odds
        except Exception:
            pass

        try:
            analysis = calculate_edge_with_claude(
                btc_price=btc_price,
                btc_start_price=market_data["start_price"],
                volatility=volatility,
                market_up_odds=market_up_odds,
                market_down_odds=market_down_odds,
                seconds_remaining=int(secs_remaining),
                question=market_data["question"]
            )
        except Exception as e:
            log.error(f"Claude error: {e}")
            return

        best_bet  = analysis.get("best_bet", "NO_BET")
        best_edge = analysis.get("best_edge", 0) * 100

        log.info(f"Analysis: {best_bet} | Edge: {best_edge:.1f}% | {secs_remaining:.0f}s left")

        if best_bet == "NO_BET" or best_edge < MIN_EDGE_PCT:
            return
        if analysis.get("confidence") == "LOW":
            return

        stake = round(self.trader.balance * BET_SIZE_PCT, 2)
        if stake < 0.10:
            return

        odds      = market_up_odds if best_bet == "UP" else market_down_odds
        true_prob = analysis.get("true_prob_up") if best_bet == "UP" else analysis.get("true_prob_down")

        bet = self.trader.place_bet(
            market_id=market_id, direction=best_bet,
            odds=odds, true_prob=true_prob, edge=best_edge, stake=stake
        )
        market_data["bet_placed"]    = True
        market_data["bet_id"]        = bet["id"]
        market_data["bet_direction"] = best_bet

        await send_trade_alert(self.bot, bet, analysis, self.trader)

    async def settle_expired_markets(self, session):
        now       = datetime.now(timezone.utc)
        to_remove = []
        for market_id, data in self.tracked_markets.items():
            if now >= data["end_time"]:
                to_remove.append(market_id)
                if not data.get("bet_placed") or not data.get("bet_id"):
                    continue
                final_price = await get_btc_price(session)
                if not final_price:
                    continue
                direction = data.get("bet_direction")
                won = (direction == "UP" and final_price >= data["start_price"]) or \
                      (direction == "DOWN" and final_price < data["start_price"])
                bet, profit = self.trader.settle_bet(data["bet_id"], won)
                if bet:
                    await send_settlement_alert(self.bot, bet, profit, self.trader)
        for m in to_remove:
            del self.tracked_markets[m]

    async def run(self):
        log.info("Polymarket Paper Trading Bot starting...")
        await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 *Polymarket Paper Trader Online*\nBalance: ${self.trader.balance:.2f} | Min edge: {MIN_EDGE_PCT}% | Bet size: {BET_SIZE_PCT*100:.0f}%\nWatching BTC 5-min up/down markets...",
            parse_mode=ParseMode.MARKDOWN
        )

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self.settle_expired_markets(session)
                    markets = await get_btc_markets(session)
                    for market in markets:
                        await self.track_market(session, market)
                    for market_id, data in list(self.tracked_markets.items()):
                        await self.evaluate_market(session, market_id, data)
                    if time.time() - self.last_status > 900:
                        btc_price = await get_btc_price(session)
                        if btc_price:
                            await send_status(self.bot, self.trader, btc_price)
                        self.last_status = time.time()
                except Exception as e:
                    log.error(f"Main loop error: {e}")
                await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    bot = PolyBot()
    asyncio.run(bot.run())
