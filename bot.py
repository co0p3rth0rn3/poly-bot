"""
Polymarket Paper Trading Bot — Bone Reaper Strategy
Uses deterministic window timestamps to find active BTC 5-min markets.
Enters at 92%+ implied probability with 8-35 seconds remaining.
Paper trades only.
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
STARTING_BALANCE   = 50.0
BET_SIZE_PCT       = 0.05   # 5% of balance per trade
ENTRY_PRICE_FLOOR  = 0.92   # Only enter when 92%+ certain
MIN_SECS_REMAINING = 8      # Don't enter in last 8s (settlement chaos)
MAX_SECS_REMAINING = 35     # Only enter in last 35s
SCAN_INTERVAL      = 2      # Scan every 2 seconds

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
# WINDOW CALCULATOR
# Markets follow fixed 300s (5-min) Unix timestamps
# ─────────────────────────────────────────
def get_current_window():
    """Calculate current active 5-min window timestamps."""
    now        = int(time.time())
    window_ts  = now - (now % 300)   # Round down to nearest 5 min
    close_time = window_ts + 300     # Window closes exactly 5 mins later
    slug       = f"btc-updown-5m-{window_ts}"
    secs_remaining = close_time - now
    return {
        "window_ts":     window_ts,
        "close_time":    close_time,
        "slug":          slug,
        "secs_remaining": secs_remaining,
        "close_dt":      datetime.fromtimestamp(close_time, tz=timezone.utc)
    }

# ─────────────────────────────────────────
# POLYMARKET — fetch market by slug
# ─────────────────────────────────────────
async def get_market_by_slug(session: aiohttp.ClientSession, slug: str) -> Optional[dict]:
    """Fetch market data using the deterministic slug."""
    url = f"https://gamma-api.polymarket.com/markets"
    params = {"slug": slug}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data    = await r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            if markets:
                log.info(f"Found market by slug: {markets[0].get('question','')}")
                return markets[0]
    except Exception as e:
        log.warning(f"Slug fetch error: {e}")

    # Fallback: try event endpoint
    try:
        url2 = f"https://gamma-api.polymarket.com/events"
        params2 = {"slug": slug}
        async with session.get(url2, params=params2, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data    = await r.json()
            events  = data if isinstance(data, list) else data.get("events", [])
            if events:
                markets = events[0].get("markets", [])
                if markets:
                    log.info(f"Found via event slug: {markets[0].get('question','')}")
                    return markets[0]
    except Exception as e:
        log.warning(f"Event slug fetch error: {e}")

    return None

# ─────────────────────────────────────────
# POLYMARKET — get live odds
# ─────────────────────────────────────────
async def get_live_odds(session: aiohttp.ClientSession, market: dict) -> Optional[dict]:
    """Get live bid prices for UP and DOWN outcomes."""
    odds = {}

    # Method 1: Use outcomePrices from gamma API (simplest)
    try:
        outcomes       = market.get("outcomes", "")
        outcome_prices = market.get("outcomePrices", "")
        if outcomes and outcome_prices:
            if isinstance(outcomes, str):
                outcomes       = [o.strip() for o in outcomes.split(",")]
                outcome_prices = [float(p.strip()) for p in outcome_prices.split(",")]
            for o, p in zip(outcomes, outcome_prices):
                odds[o.upper()] = float(p)
            if odds:
                log.info(f"Odds from gamma API: {odds}")
                return odds
    except Exception as e:
        log.warning(f"Gamma price parse error: {e}")

    # Method 2: CLOB orderbook
    tokens = market.get("tokens", [])
    condition_id = market.get("conditionId") or market.get("id", "")

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

    for token in tokens:
        outcome  = str(token.get("outcome", "")).upper()
        token_id = token.get("token_id", "")
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
            return float((await r.json())["price"])
    except Exception:
        try:
            async with session.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                return float((await r.json())["data"]["amount"])
        except Exception:
            return None

# ─────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────
async def send_trade_alert(bot, bet, question, secs, trader):
    emoji   = "📈" if "UP" in bet["direction"] else "📉"
    pnl_str = f"+${trader.pnl:.2f}" if trader.pnl >= 0 else f"-${abs(trader.pnl):.2f}"
    msg = f"""{emoji} *PAPER TRADE — Bone Reaper*

*Direction:* {bet['direction']}
*Entry:* {bet['entry_price']*100:.1f}% implied prob
*Stake:* ${bet['stake']:.2f}
*Potential Profit:* ${bet['potential_profit']:.4f}
*Time Left:* {secs:.0f}s

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
*Record:* {trader.wins}W / {trader.losses}L | *Win Rate:* {trader.win_rate:.0f}%
*Skipped:* {trader.skipped} (no edge)"""
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
        self.current_window_ts = None
        self.market_data       = None
        self.bet_placed        = False
        self.last_status       = 0

    async def run(self):
        log.info("Bone Reaper Paper Bot starting...")
        await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 *Bone Reaper Paper Bot*\nBalance: ${self.trader.balance:.2f}\nEntering at {ENTRY_PRICE_FLOOR*100:.0f}%+ with {MIN_SECS_REMAINING}-{MAX_SECS_REMAINING}s left\nScanning every {SCAN_INTERVAL}s",
            parse_mode=ParseMode.MARKDOWN
        )

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    window = get_current_window()
                    secs   = window["secs_remaining"]

                    # New window started — reset
                    if window["window_ts"] != self.current_window_ts:
                        # Settle any open bets from previous window
                        for bet in list(self.trader.open_bets):
                            won = random.random() < bet["entry_price"]  # Simulate based on entry prob
                            settled, profit = self.trader.settle_bet(bet["id"], won)
                            if settled:
                                await send_settlement_alert(self.bot, settled, profit, self.trader)

                        self.current_window_ts = window["window_ts"]
                        self.bet_placed        = False
                        self.market_data       = None
                        log.info(f"New window: {window['slug']} | {secs:.0f}s remaining")

                    # Fetch market data once per window if we don't have it
                    if self.market_data is None:
                        self.market_data = await get_market_by_slug(session, window["slug"])
                        if self.market_data:
                            log.info(f"Market loaded: {self.market_data.get('question','')[:60]}")
                        else:
                            log.info(f"Market not found for slug: {window['slug']} — will retry")

                    # Check entry window
                    if not self.bet_placed and self.market_data:
                        if secs < MIN_SECS_REMAINING:
                            log.info(f"Too late — {secs:.0f}s left")
                        elif secs > MAX_SECS_REMAINING:
                            log.info(f"Waiting — {secs:.0f}s left (entry: {MIN_SECS_REMAINING}-{MAX_SECS_REMAINING}s)")
                        else:
                            # IN THE ENTRY WINDOW — get odds and decide
                            log.info(f"Entry window! {secs:.0f}s left — fetching odds...")
                            odds = await get_live_odds(session, self.market_data)
                            log.info(f"Odds: {odds}")

                            if not odds:
                                log.info("No odds — skipping")
                                self.trader.skipped += 1
                            else:
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
                                    stake    = round(self.trader.balance * BET_SIZE_PCT, 2)
                                    question = self.market_data.get("question", "")
                                    if stake >= 0.10:
                                        bet = self.trader.place_bet(
                                            market_id=window["slug"],
                                            direction=best_direction,
                                            entry_price=best_price,
                                            stake=stake
                                        )
                                        self.bet_placed = True
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
