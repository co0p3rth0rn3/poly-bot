"""
Polymarket Paper Trading Bot — Bone Reaper Strategy
Scalps late-window mispricings on BTC 5-minute up/down markets.
Only enters when market is 92%+ certain with 8-35 seconds left.
Paper trades only — no real money at risk.
"""

import asyncio
import aiohttp
import json
import os
import time
import logging
import random
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
# BONE REAPER STRATEGY SETTINGS
# ─────────────────────────────────────────
STARTING_BALANCE    = 50.0
BET_SIZE_PCT        = 0.05    # 5% of balance per trade
ENTRY_PRICE_FLOOR   = 0.92    # Only enter when 92%+ certain
MIN_SECS_REMAINING  = 8       # Don't enter in last 8s
MAX_SECS_REMAINING  = 35      # Only enter in last 35s
MAX_MARKET_DURATION = 600     # Only consider markets under 10 minutes total duration
SCAN_INTERVAL       = 2       # Scan every 2 seconds

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
        self.skipped       = 0

    @property
    def pnl(self):
        return self.balance - self.start_balance

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    def place_bet(self, market_id, direction, entry_price, stake):
        payout           = stake / entry_price
        potential_profit = payout - stake
        bet = {
            "id":               len(self.trades) + 1,
            "market_id":        market_id,
            "direction":        direction,
            "entry_price":      entry_price,
            "stake":            stake,
            "payout":           payout,
            "potential_profit": potential_profit,
            "time":             datetime.now(timezone.utc).isoformat(),
            "status":           "open"
        }
        self.balance -= stake
        self.open_bets.append(bet)
        self.trades.append(bet)
        return bet

    def settle_bet(self, bet_id, won: bool):
        for bet in self.open_bets:
            if bet["id"] == bet_id:
                if won:
                    self.balance += bet["payout"]
                    profit = bet["potential_profit"]
                    self.wins += 1
                else:
                    profit = -bet["stake"]
                    self.losses += 1
                bet["status"] = "won" if won else "lost"
                bet["profit"] = profit
                self.open_bets.remove(bet)
                return bet, profit
        return None, 0

# ─────────────────────────────────────────
# POLYMARKET — fetch active 5-min BTC market
# ─────────────────────────────────────────
async def get_current_btc_market(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Fetch ONLY the active 5-minute BTC up/down market.
    Filters out long-term BTC markets (hitting $1M etc).
    """
    url    = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": 100}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data    = await r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])

        now = datetime.now(timezone.utc)
        candidates = []

        for m in markets:
            q = (m.get("question", "") + m.get("description", "")).lower()

            # Must mention BTC/bitcoin
            if "bitcoin" not in q and "btc" not in q:
                continue

            # Must be an up/down market
            if not any(w in q for w in ["up", "down", "higher", "lower", "above", "below"]):
                continue

            # Must have an end time
            end_time = m.get("endDate") or m.get("end_date_iso", "")
            if not end_time:
                continue

            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                secs_remaining = (end_dt - now).total_seconds()

                # KEY FILTER: only markets ending within 10 minutes
                # This eliminates "Will BTC hit $1M" style long-term markets
                if secs_remaining < 0 or secs_remaining > MAX_MARKET_DURATION:
                    log.info(f"Skipping long-term market: '{m.get('question','')[:50]}' ({secs_remaining:.0f}s)")
                    continue

                m["_secs_remaining"] = secs_remaining
                m["_end_dt"]         = end_dt
                candidates.append(m)
                log.info(f"Candidate: '{m.get('question','')[:60]}' | {secs_remaining:.0f}s remaining")

            except Exception as e:
                log.warning(f"Date parse error: {e}")
                continue

        if not candidates:
            log.info("No active 5-min BTC market found")
            return None

        # Return the one closest to ending (most urgent)
        return min(candidates, key=lambda x: x["_secs_remaining"])

    except Exception as e:
        log.warning(f"Market fetch error: {e}")
        return None

# ─────────────────────────────────────────
# POLYMARKET — get live odds
# ─────────────────────────────────────────
async def get_live_odds(session: aiohttp.ClientSession, market: dict) -> Optional[dict]:
    tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
    condition_id = market.get("conditionId") or market.get("id", "")

    # Try CLOB market endpoint first
    if not tokens:
        try:
            async with session.get(
                f"https://clob.polymarket.com/markets/{condition_id}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                clob_data = await r.json()
                tokens    = clob_data.get("tokens", [])
        except Exception:
            pass

    odds = {}
    for token in tokens:
        outcome  = str(token.get("outcome", "")).upper()
        token_id = token.get("token_id", "") or str(token)
        if not token_id or not outcome:
            continue
        try:
            async with session.get(
                f"https://clob.polymarket.com/book?token_id={token_id}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                book = await r.json()
                bids = book.get("bids", [])
                if bids:
                    odds[outcome] = float(bids[0]["price"])
        except Exception as e:
            log.warning(f"Book fetch error: {e}")

    # Fallback: try midpoint from gamma API
    if not odds:
        try:
            outcomes       = market.get("outcomes", "").split(",") if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
            outcome_prices = market.get("outcomePrices", "").split(",") if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])
            for o, p in zip(outcomes, outcome_prices):
                odds[o.strip().upper()] = float(p.strip())
            if odds:
                log.info(f"Using gamma API prices: {odds}")
        except Exception:
            pass

    return odds if odds else None

# ─────────────────────────────────────────
# BTC PRICE
# ─────────────────────────────────────────
async def get_btc_price(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            timeout=aiohttp.ClientTimeout(total=3)
        ) as r:
            data = await r.json()
            return float(data["price"])
    except Exception:
        try:
            async with session.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                data = await r.json()
                return float(data["data"]["amount"])
        except Exception:
            return None

# ─────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────
async def send_trade_alert(bot, bet, question, secs_remaining, trader):
    emoji   = "📈" if bet["direction"] == "UP" else "📉"
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{emoji} *PAPER TRADE — Bone Reaper*

*Direction:* {bet['direction']}
*Entry:* {bet['entry_price']*100:.1f}% implied prob
*Stake:* ${bet['stake']:.2f}
*Potential Profit:* ${bet['potential_profit']:.3f}
*Time Left:* {secs_remaining:.0f}s

_{question[:80]}_

*Balance:* ${trader.balance:.2f} | *P&L:* {pnl_str}"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_settlement_alert(bot, bet, profit, trader):
    emoji      = "✅" if profit > 0 else "❌"
    result     = "WON" if profit > 0 else "LOST"
    profit_str = f"+${profit:.4f}" if profit >= 0 else f"-${abs(profit):.4f}"
    pnl_str    = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{emoji} *SETTLED — {result}*

*Direction:* {bet['direction']}
*Entry:* {bet['entry_price']*100:.1f}%
*Profit:* {profit_str}

*Balance:* ${trader.balance:.2f} | *P&L:* {pnl_str}
*Record:* {trader.wins}W / {trader.losses}L | *Win Rate:* {trader.win_rate:.0f}%"""
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
*Skipped:* {trader.skipped} (no edge)
*Time:* {datetime.now(timezone.utc).strftime('%H:%M UTC')}"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────
class PolyBot:
    def __init__(self):
        self.bot               = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader            = PaperTrader()
        self.current_market    = None
        self.market_bet_placed = False
        self.last_status       = 0
        self.last_market_fetch = 0

    async def run(self):
        log.info("Bone Reaper Paper Bot starting...")
        await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 *Bone Reaper Paper Bot*\nBalance: ${self.trader.balance:.2f}\nEnter at {ENTRY_PRICE_FLOOR*100:.0f}%+ implied prob with {MIN_SECS_REMAINING}-{MAX_SECS_REMAINING}s left\nOnly trades 5-min BTC markets",
            parse_mode=ParseMode.MARKDOWN
        )

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    now = datetime.now(timezone.utc)

                    # Fetch market every 30s
                    if time.time() - self.last_market_fetch > 30 or self.current_market is None:
                        market = await get_current_btc_market(session)
                        if market:
                            market_id = market.get("conditionId") or market.get("id", "")
                            cur_id    = self.current_market.get("conditionId") or self.current_market.get("id", "") if self.current_market else ""
                            if market_id != cur_id:
                                self.current_market    = market
                                self.market_bet_placed = False
                                log.info(f"New market: {market.get('question','')[:60]} | {market['_secs_remaining']:.0f}s")
                        else:
                            self.current_market = None
                        self.last_market_fetch = time.time()

                    # Settle expired
                    if self.current_market and self.market_bet_placed:
                        end_dt = self.current_market.get("_end_dt")
                        if end_dt and now >= end_dt:
                            for bet in list(self.trader.open_bets):
                                mid = self.current_market.get("conditionId") or self.current_market.get("id", "")
                                if bet["market_id"] == mid:
                                    won = random.random() < bet["entry_price"]  # Simulate based on entry probability
                                    settled, profit = self.trader.settle_bet(bet["id"], won)
                                    if settled:
                                        await send_settlement_alert(self.bot, settled, profit, self.trader)
                            self.current_market    = None
                            self.market_bet_placed = False

                    # Evaluate entry
                    if self.current_market and not self.market_bet_placed:
                        end_dt = self.current_market.get("_end_dt")
                        if not end_dt:
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        # Recalculate fresh time remaining
                        self.current_market["_secs_remaining"] = (end_dt - now).total_seconds()
                        secs = self.current_market["_secs_remaining"]

                        if secs < MIN_SECS_REMAINING:
                            log.info(f"Too late — {secs:.0f}s left")
                        elif secs > MAX_SECS_REMAINING:
                            log.info(f"Waiting — {secs:.0f}s left (entry window: {MIN_SECS_REMAINING}-{MAX_SECS_REMAINING}s)")
                        else:
                            # IN THE ENTRY WINDOW
                            log.info(f"Entry window! {secs:.0f}s left — checking odds...")
                            odds = await get_live_odds(session, self.current_market)
                            log.info(f"Odds: {odds}")

                            if not odds:
                                log.info("No odds available — skipping")
                                self.trader.skipped += 1
                            else:
                                # Find best side above floor
                                best_direction = None
                                best_price     = 0.0
                                for outcome, price in odds.items():
                                    if price >= ENTRY_PRICE_FLOOR and price > best_price:
                                        best_direction = outcome
                                        best_price     = price

                                if not best_direction:
                                    log.info(f"No side above {ENTRY_PRICE_FLOOR*100:.0f}% — skipping. Odds: {odds}")
                                    self.trader.skipped += 1
                                else:
                                    stake     = round(self.trader.balance * BET_SIZE_PCT, 2)
                                    market_id = self.current_market.get("conditionId") or self.current_market.get("id", "")
                                    question  = self.current_market.get("question", "")

                                    if stake >= 0.10:
                                        bet = self.trader.place_bet(
                                            market_id=market_id,
                                            direction=best_direction,
                                            entry_price=best_price,
                                            stake=stake
                                        )
                                        self.market_bet_placed = True
                                        log.info(f"BET: {best_direction} @ {best_price*100:.1f}% | ${stake:.2f} | {secs:.0f}s left")
                                        await send_trade_alert(self.bot, bet, question, secs, self.trader)

                    # Status every 15 mins
                    if time.time() - self.last_status > 900:
                        btc = await get_btc_price(session)
                        if btc:
                            await send_status(self.bot, self.trader, btc)
                        self.last_status = time.time()

                except Exception as e:
                    log.error(f"Loop error: {e}")

                await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    bot = PolyBot()
    asyncio.run(bot.run())
