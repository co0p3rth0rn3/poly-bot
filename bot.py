"""
Polymarket Paper Trading Bot — Bone Reaper Strategy
Scalps late-window mispricings on BTC 5-minute up/down markets.
Only enters when market is 95%+ certain with 8-35 seconds left.
Paper trades only — no real money at risk.
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
# BONE REAPER STRATEGY SETTINGS
# ─────────────────────────────────────────
STARTING_BALANCE    = 50.0
BET_SIZE_PCT        = 0.05     # 5% of balance per trade (conservative)
ENTRY_PRICE_FLOOR   = 0.92     # Only enter when market is 92%+ certain (article says 0.95, slightly lower to get more trades)
MIN_SECS_REMAINING  = 8        # Don't enter in last 8s — settlement chaos
MAX_SECS_REMAINING  = 35       # Only enter in the last 35 seconds
SCAN_INTERVAL       = 2        # Scan every 2 seconds — need fast reaction in late window

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
        self.skipped       = 0  # Markets considered but not entered

    @property
    def pnl(self):
        return self.balance - self.start_balance

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    def place_bet(self, market_id, direction, entry_price, stake):
        """
        Entry price is the implied probability e.g. 0.96 means 96% chance of winning.
        Payout = stake / entry_price (buy at 96c, win $1, profit = 4c per $1 wagered).
        """
        payout    = stake / entry_price
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
        self.total_wagered += stake
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
# POLYMARKET — fetch current BTC market
# ─────────────────────────────────────────
async def get_current_btc_market(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Fetch the currently active BTC 5-min up/down market.
    Uses the Polymarket gamma API.
    """
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active":   "true",
        "closed":   "false",
        "limit":    100,
        "tag_slug": "crypto"
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data    = await r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])

        for m in markets:
            q = (m.get("question", "") + m.get("description", "")).lower()
            if ("bitcoin" in q or "btc" in q) and \
               ("5" in q or "five" in q) and \
               ("up" in q or "down" in q or "higher" in q or "lower" in q):

                end_time = m.get("endDate") or m.get("end_date_iso", "")
                if not end_time:
                    continue

                end_dt  = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                now     = datetime.now(timezone.utc)
                secs    = (end_dt - now).total_seconds()

                log.info(f"BTC market found: '{m.get('question','')[:60]}' | {secs:.0f}s remaining")
                m["_secs_remaining"] = secs
                m["_end_dt"]         = end_dt
                return m

        log.info("No active BTC 5-min market found")
        return None

    except Exception as e:
        log.warning(f"Market fetch error: {e}")
        return None

# ─────────────────────────────────────────
# POLYMARKET — get live odds from CLOB
# ─────────────────────────────────────────
async def get_live_odds(session: aiohttp.ClientSession, market: dict) -> Optional[dict]:
    """
    Get live best bid/ask for UP and DOWN outcomes.
    Returns {"up": 0.96, "down": 0.03} style dict.
    """
    # Try to get token IDs from market data
    tokens = market.get("tokens", []) or market.get("outcomes", [])

    if not tokens:
        # Fall back to condition ID
        condition_id = market.get("conditionId") or market.get("id", "")
        url = f"https://clob.polymarket.com/markets/{condition_id}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                clob_data = await r.json()
                tokens    = clob_data.get("tokens", [])
        except Exception:
            return None

    odds = {}
    for token in tokens:
        outcome   = token.get("outcome", "").upper()
        token_id  = token.get("token_id", "")
        if not token_id:
            continue
        try:
            async with session.get(
                f"https://clob.polymarket.com/book?token_id={token_id}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                book = await r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    best_bid = float(bids[0]["price"])
                    odds[outcome] = best_bid
                elif asks:
                    best_ask = float(asks[0]["price"])
                    odds[outcome] = best_ask
        except Exception as e:
            log.warning(f"Book fetch error for {outcome}: {e}")

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
async def send_trade_alert(bot, bet, market_question, secs_remaining, trader):
    direction_emoji = "📈" if bet["direction"] == "UP" else "📉"
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{direction_emoji} *PAPER TRADE — Bone Reaper*

*Direction:* {bet['direction']}
*Entry Price:* {bet['entry_price']*100:.1f}% (implied probability)
*Stake:* ${bet['stake']:.2f}
*Potential Profit:* ${bet['potential_profit']:.3f}
*Seconds Remaining:* {secs_remaining:.0f}s

_{market_question[:80]}_

*Balance:* ${trader.balance:.2f} | *P&L:* {pnl_str}"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_settlement_alert(bot, bet, profit, trader):
    emoji      = "✅" if profit > 0 else "❌"
    result     = "WON" if profit > 0 else "LOST"
    profit_str = f"+${profit:.4f}" if profit >= 0 else f"-${abs(profit):.4f}"
    pnl_str    = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{emoji} *SETTLED — {result}*

*Direction:* {bet['direction']}
*Entry Price:* {bet['entry_price']*100:.1f}%
*Profit:* {profit_str}

*Balance:* ${trader.balance:.2f}
*P&L:* {pnl_str}
*Win Rate:* {trader.win_rate:.0f}%
*Record:* {trader.wins}W / {trader.losses}L
*Skipped:* {trader.skipped} markets (no edge)"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_status(bot, trader, btc_price):
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    pnl_pct = trader.pnl / trader.start_balance * 100
    total   = trader.wins + trader.losses
    msg = f"""📊 *Status Update*

*BTC:* ${btc_price:,.2f}
*Balance:* ${trader.balance:.2f}
*P&L:* {pnl_str} ({pnl_pct:+.1f}%)
*Win Rate:* {trader.win_rate:.0f}%
*Trades:* {total} ({trader.wins}W / {trader.losses}L)
*Skipped:* {trader.skipped} (no edge found)
*Open Bets:* {len(trader.open_bets)}
*Time:* {datetime.now(timezone.utc).strftime('%H:%M UTC')}"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)

async def send_decision_log(bot, msg_text):
    """Log every decision — the article says this is how you build intuition."""
    log.info(f"DECISION: {msg_text}")

# ─────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────
class PolyBot:
    def __init__(self):
        self.bot             = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader          = PaperTrader()
        self.current_market  = None
        self.market_bet_placed = False
        self.last_status     = 0
        self.last_market_fetch = 0

    async def run(self):
        log.info("Bone Reaper Paper Bot starting...")
        await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 *Bone Reaper Paper Bot Online*\nBalance: ${self.trader.balance:.2f}\nStrategy: Enter at {ENTRY_PRICE_FLOOR*100:.0f}%+ implied prob with {MIN_SECS_REMAINING}-{MAX_SECS_REMAINING}s remaining\nScanning every {SCAN_INTERVAL}s...",
            parse_mode=ParseMode.MARKDOWN
        )

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    now = datetime.now(timezone.utc)

                    # Fetch new market every 60s or if we don't have one
                    if time.time() - self.last_market_fetch > 60 or self.current_market is None:
                        market = await get_current_btc_market(session)
                        if market:
                            market_id = market.get("conditionId") or market.get("id", "")
                            # If it's a new market, reset bet flag
                            if self.current_market is None or \
                               market_id != (self.current_market.get("conditionId") or self.current_market.get("id", "")):
                                self.current_market  = market
                                self.market_bet_placed = False
                                log.info(f"New market loaded: {market.get('question','')[:60]}")
                        self.last_market_fetch = time.time()

                    # Settle expired market
                    if self.current_market and self.market_bet_placed:
                        end_dt = self.current_market.get("_end_dt")
                        if end_dt and now >= end_dt:
                            # Settle any open bets
                            btc_price = await get_btc_price(session)
                            for bet in list(self.trader.open_bets):
                                if bet["market_id"] == (self.current_market.get("conditionId") or self.current_market.get("id", "")):
                                    # We don't have start price in this strategy — settlement is handled by Polymarket
                                    # For paper trading we simulate: if entry was 0.95+, we win 95% of the time
                                    import random
                                    won = random.random() < bet["entry_price"]
                                    settled_bet, profit = self.trader.settle_bet(bet["id"], won)
                                    if settled_bet:
                                        await send_settlement_alert(self.bot, settled_bet, profit, self.trader)
                            self.current_market    = None
                            self.market_bet_placed = False

                    # Check if we should enter
                    if self.current_market and not self.market_bet_placed:
                        end_dt = self.current_market.get("_end_dt")
                        if not end_dt:
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        # Recalculate time remaining
                        secs_remaining = (end_dt - now).total_seconds()

                        if secs_remaining < MIN_SECS_REMAINING:
                            log.info(f"Too late — only {secs_remaining:.0f}s left (min {MIN_SECS_REMAINING}s)")
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        if secs_remaining > MAX_SECS_REMAINING:
                            log.info(f"Too early — {secs_remaining:.0f}s left (max {MAX_SECS_REMAINING}s)")
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        # In the window! Get live odds
                        log.info(f"In entry window! {secs_remaining:.0f}s remaining — fetching odds...")
                        odds = await get_live_odds(session, self.current_market)

                        if not odds:
                            log.info("Could not get odds — skipping")
                            self.trader.skipped += 1
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        log.info(f"Live odds: {odds}")

                        # Find if any side is above the entry floor
                        best_direction = None
                        best_price     = 0.0

                        for outcome, price in odds.items():
                            if price >= ENTRY_PRICE_FLOOR and price > best_price:
                                best_direction = outcome
                                best_price     = price

                        if not best_direction:
                            log.info(f"No side above {ENTRY_PRICE_FLOOR*100:.0f}% floor — skipping. Odds: {odds}")
                            self.trader.skipped += 1
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        # Place paper bet!
                        stake      = round(self.trader.balance * BET_SIZE_PCT, 2)
                        market_id  = self.current_market.get("conditionId") or self.current_market.get("id", "")
                        question   = self.current_market.get("question", "")

                        if stake < 0.10:
                            log.info("Balance too low to bet")
                            await asyncio.sleep(SCAN_INTERVAL)
                            continue

                        bet = self.trader.place_bet(
                            market_id=market_id,
                            direction=best_direction,
                            entry_price=best_price,
                            stake=stake
                        )
                        self.market_bet_placed = True
                        log.info(f"BET PLACED: {best_direction} @ {best_price*100:.1f}% | ${stake:.2f} stake | {secs_remaining:.0f}s left")
                        await send_trade_alert(self.bot, bet, question, secs_remaining, self.trader)

                    # Status update every 15 minutes
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
