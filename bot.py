"""
Polymarket Paper Trading Bot
Watches short-term BTC directional markets on Polymarket,
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

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# ENV VARIABLES
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "YOUR_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "YOUR_CHAT_ID"
)

ANTHROPIC_API_KEY = os.getenv(
    "ANTHROPIC_API_KEY",
    "YOUR_ANTHROPIC_KEY"
)

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────
STARTING_BALANCE = 50.0

BET_SIZE_PCT = 0.05
MIN_EDGE_PCT = 5.0

SCAN_INTERVAL = 30

MIN_TIME_REMAINING = 30
MAX_TIME_REMAINING = 600

# ─────────────────────────────────────────
# PAPER TRADER
# ─────────────────────────────────────────
class PaperTrader:

    def __init__(self):

        self.balance = STARTING_BALANCE
        self.start_balance = STARTING_BALANCE

        self.trades = []
        self.open_bets = []

        self.wins = 0
        self.losses = 0

        self.total_wagered = 0.0

    @property
    def pnl(self):
        return self.balance - self.start_balance

    @property
    def win_rate(self):

        total = self.wins + self.losses

        if total == 0:
            return 0

        return (self.wins / total) * 100

    def place_bet(
        self,
        market_id,
        direction,
        odds,
        true_prob,
        edge,
        stake
    ):

        bet = {
            "id": len(self.trades) + 1,
            "market_id": market_id,
            "direction": direction,
            "odds": odds,
            "true_prob": true_prob,
            "edge": edge,
            "stake": stake,
            "time": datetime.now(timezone.utc).isoformat(),
            "status": "open"
        }

        self.balance -= stake

        self.total_wagered += stake

        self.trades.append(bet)
        self.open_bets.append(bet)

        return bet

    def settle_bet(self, bet_id, won: bool):

        for bet in self.open_bets:

            if bet["id"] != bet_id:
                continue

            self.open_bets.remove(bet)

            if won:

                payout = bet["stake"] / bet["odds"]

                self.balance += bet["stake"] + payout

                self.wins += 1

                profit = payout

                bet["status"] = "won"

            else:

                self.losses += 1

                profit = -bet["stake"]

                bet["status"] = "lost"

            bet["profit"] = profit

            return bet, profit

        return None, 0

# ─────────────────────────────────────────
# POLYMARKET MARKET FETCHER
# ─────────────────────────────────────────
async def get_btc_markets(
    session: aiohttp.ClientSession
) -> list:

    url = "https://gamma-api.polymarket.com/markets"

    params = {
        "active": "true",
        "closed": "false",
        "limit": 200
    }

    try:

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:

            data = await r.json()

            markets = (
                data if isinstance(data, list)
                else data.get("markets", [])
            )

            btc_markets = []

            for m in markets:

                question = (
                    m.get("question", "") +
                    " " +
                    m.get("description", "")
                ).lower()

                # ─────────────────────────
                # BTC keyword required
                # ─────────────────────────
                has_btc = (
                    "bitcoin" in question or
                    "btc" in question
                )

                # ─────────────────────────
                # Short-term directional
                # ─────────────────────────
                is_short_term = (
                    "5 minute" in question or
                    "5-minute" in question or
                    "5min" in question or
                    "higher" in question or
                    "lower" in question or
                    "above" in question or
                    "below" in question or
                    "up" in question or
                    "down" in question
                )

                # ─────────────────────────
                # Reject junk long-term
                # ─────────────────────────
                is_bad_market = (
                    "$1m" in question or
                    "million" in question or
                    "gta" in question or
                    "before" in question or
                    "2026" in question or
                    "2027" in question or
                    "president" in question or
                    "etf" in question
                )

                if has_btc and is_short_term and not is_bad_market:
                    btc_markets.append(m)

            log.info(f"Filtered BTC markets: {len(btc_markets)}")

            now = datetime.now(timezone.utc)

            for m in btc_markets:

                end = (
                    m.get("endDate")
                    or m.get("end_date_iso")
                    or "N/A"
                )

                try:

                    end_dt = datetime.fromisoformat(
                        end.replace("Z", "+00:00")
                    )

                    secs = (end_dt - now).total_seconds()

                    log.info(
                        f"MATCHED MARKET: "
                        f"{m.get('question', '')[:80]} | "
                        f"ends in {secs:.0f}s"
                    )

                except Exception:

                    log.info(
                        f"MATCHED MARKET: "
                        f"{m.get('question', '')[:80]}"
                    )

            return btc_markets

    except Exception as e:

        log.warning(f"Polymarket fetch error: {e}")

        return []

# ─────────────────────────────────────────
# ORDERBOOK
# ─────────────────────────────────────────
async def get_market_orderbook(
    session: aiohttp.ClientSession,
    condition_id: str
) -> dict:

    url = "https://clob.polymarket.com/book"

    params = {
        "token_id": condition_id
    }

    try:

        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:

            return await r.json()

    except Exception as e:

        log.warning(f"Orderbook fetch error: {e}")

        return {}

# ─────────────────────────────────────────
# BTC PRICE
# ─────────────────────────────────────────
async def get_btc_price(
    session: aiohttp.ClientSession
) -> Optional[float]:

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

# ─────────────────────────────────────────
# BTC VOLATILITY
# ─────────────────────────────────────────
async def get_btc_volatility(
    session: aiohttp.ClientSession
) -> float:

    try:

        async with session.get(
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=20",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:

            candles = await r.json()

            closes = [float(c[4]) for c in candles]

            if len(closes) < 2:
                return 0.5

            returns = []

            for i in range(1, len(closes)):

                change = (
                    (closes[i] - closes[i - 1])
                    / closes[i - 1]
                ) * 100

                returns.append(change)

            mean = sum(returns) / len(returns)

            variance = sum(
                (r - mean) ** 2 for r in returns
            ) / len(returns)

            return variance ** 0.5

    except Exception:

        return 0.5

# ─────────────────────────────────────────
# CLAUDE EDGE CALCULATOR
# ─────────────────────────────────────────
def calculate_edge_with_claude(
    btc_price,
    btc_start_price,
    volatility,
    market_up_odds,
    market_down_odds,
    seconds_remaining,
    question
):

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY
    )

    price_change_pct = (
        (btc_price - btc_start_price)
        / btc_start_price
    ) * 100

    prompt = f"""
You are a quantitative trader specialising in
Polymarket prediction markets.

CURRENT MARKET:
{question}

BTC PRICE NOW:
${btc_price:,.2f}

BTC START PRICE:
${btc_start_price:,.2f}

PRICE CHANGE:
{price_change_pct:+.3f}%

VOLATILITY:
{volatility:.4f}%

SECONDS REMAINING:
{seconds_remaining}

MARKET ODDS:
UP = {market_up_odds*100:.1f}%
DOWN = {market_down_odds*100:.1f}%

Calculate TRUE probability.

Return ONLY valid JSON:

{{
  "true_prob_up": 0.52,
  "true_prob_down": 0.48,
  "edge_up": 0.07,
  "edge_down": -0.07,
  "best_bet": "UP",
  "best_edge": 0.07,
  "reasoning": "Momentum favours upside.",
  "confidence": "HIGH"
}}
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    raw = (
        message.content[0]
        .text
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    return json.loads(raw)

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
async def send_trade_alert(
    bot,
    trade,
    analysis,
    trader
):

    emoji = "📈" if trade["direction"] == "UP" else "📉"

    pnl = (
        f"+${trader.pnl:.2f}"
        if trader.pnl >= 0
        else f"-${abs(trader.pnl):.2f}"
    )

    msg = f"""
{emoji} PAPER TRADE

Direction: {trade['direction']}
Odds: {trade['odds']*100:.1f}%
Edge: +{trade['edge']:.1f}%

Stake: ${trade['stake']:.2f}

Balance: ${trader.balance:.2f}
P&L: {pnl}

Reason:
{analysis.get('reasoning', '')}
"""

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg
    )

async def send_status(
    bot,
    trader,
    btc_price
):

    pnl = (
        f"+${trader.pnl:.2f}"
        if trader.pnl >= 0
        else f"-${abs(trader.pnl):.2f}"
    )

    msg = f"""
📊 STATUS

BTC:
${btc_price:,.2f}

Balance:
${trader.balance:.2f}

P&L:
{pnl}

Win Rate:
{trader.win_rate:.0f}%

Trades:
{trader.wins + trader.losses}
"""

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg
    )

# ─────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────
class PolyBot:

    def __init__(self):

        self.bot = Bot(
            token=TELEGRAM_BOT_TOKEN
        )

        self.trader = PaperTrader()

        self.tracked_markets = {}

        self.last_status = 0

    async def track_market(
        self,
        session,
        market
    ):

        market_id = (
            market.get("conditionId")
            or market.get("id", "")
        )

        question = market.get("question", "")

        if market_id in self.tracked_markets:
            return

        end_time = (
            market.get("endDate")
            or market.get("end_date_iso")
        )

        if not end_time:
            return

        try:

            end_dt = datetime.fromisoformat(
                end_time.replace("Z", "+00:00")
            )

            now = datetime.now(timezone.utc)

            secs_remaining = (
                end_dt - now
            ).total_seconds()

            log.info(
                f"Market time check: "
                f"{secs_remaining:.0f}s remaining"
            )

            if (
                secs_remaining < MIN_TIME_REMAINING
                or secs_remaining > MAX_TIME_REMAINING
            ):

                log.info(
                    "Skipping market outside time window"
                )

                return

            btc_price = await get_btc_price(session)

            if not btc_price:
                return

            self.tracked_markets[market_id] = {
                "start_price": btc_price,
                "end_time": end_dt,
                "question": question,
                "bet_placed": False
            }

            log.info(
                f"TRACKING MARKET: "
                f"{question[:80]}"
            )

        except Exception as e:

            log.warning(f"Track error: {e}")

    async def evaluate_market(
        self,
        session,
        market_id,
        market_data
    ):

        if market_data.get("bet_placed"):
            return

        now = datetime.now(timezone.utc)

        secs_remaining = (
            market_data["end_time"] - now
        ).total_seconds()

        if (
            secs_remaining < MIN_TIME_REMAINING
            or secs_remaining > MAX_TIME_REMAINING
        ):
            return

        btc_price = await get_btc_price(session)

        volatility = await get_btc_volatility(session)

        if not btc_price:
            return

        market_up_odds = 0.50
        market_down_odds = 0.50

        try:

            book = await get_market_orderbook(
                session,
                market_id
            )

            if book and book.get("bids"):

                market_up_odds = float(
                    book["bids"][0]["price"]
                )

                market_down_odds = (
                    1 - market_up_odds
                )

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

        best_bet = analysis.get(
            "best_bet",
            "NO_BET"
        )

        best_edge = (
            analysis.get("best_edge", 0)
            * 100
        )

        log.info(
            f"Analysis: "
            f"{best_bet} | "
            f"Edge: {best_edge:.1f}%"
        )

        if (
            best_bet == "NO_BET"
            or best_edge < MIN_EDGE_PCT
        ):
            return

        if analysis.get("confidence") == "LOW":
            return

        stake = round(
            self.trader.balance * BET_SIZE_PCT,
            2
        )

        if stake < 0.10:
            return

        odds = (
            market_up_odds
            if best_bet == "UP"
            else market_down_odds
        )

        true_prob = (
            analysis.get("true_prob_up")
            if best_bet == "UP"
            else analysis.get("true_prob_down")
        )

        bet = self.trader.place_bet(
            market_id=market_id,
            direction=best_bet,
            odds=odds,
            true_prob=true_prob,
            edge=best_edge,
            stake=stake
        )

        market_data["bet_placed"] = True

        await send_trade_alert(
            self.bot,
            bet,
            analysis,
            self.trader
        )

    async def run(self):

        log.info(
            "Polymarket Paper Trading Bot starting..."
        )

        await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "🤖 Polymarket Paper Trader Online\n"
                f"Balance: ${self.trader.balance:.2f}\n"
                f"Min Edge: {MIN_EDGE_PCT}%\n"
                f"Bet Size: {BET_SIZE_PCT*100:.0f}%"
            )
        )

        async with aiohttp.ClientSession() as session:

            while True:

                try:

                    markets = await get_btc_markets(
                        session
                    )

                    for market in markets:

                        await self.track_market(
                            session,
                            market
                        )

                    for market_id, data in list(
                        self.tracked_markets.items()
                    ):

                        await self.evaluate_market(
                            session,
                            market_id,
                            data
                        )

                    if (
                        time.time() - self.last_status
                    ) > 900:

                        btc_price = await get_btc_price(
                            session
                        )

                        if btc_price:

                            await send_status(
                                self.bot,
                                self.trader,
                                btc_price
                            )

                        self.last_status = time.time()

                except Exception as e:

                    log.error(
                        f"Main loop error: {e}"
                    )

                await asyncio.sleep(
                    SCAN_INTERVAL
                )

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
if __name__ == "__main__":

    bot = PolyBot()

    asyncio.run(bot.run())
