"""
Estrategia EMA50 multitemporal (1H -> 5M -> 1M), sin repintado, solo velas cerradas.

Flujo:
1) En 1H: detecta que una vela TOCA (con mecha) la EMA50.
2) En 5M: a partir de ese toque, busca ruptura (cierre por encima/debajo de la EMA50)
   y luego un retest válido (vela que toca la EMA50 y cierra de nuevo en la misma
   dirección de la ruptura).
3) En 1M: en paralelo a la fase de retest de 5M, repite el mismo esquema
   (ruptura + retest) como confirmación final.
4) Cuando 5M Y 1M completan su retest -> señal BUY o SELL -> notificación push.

Fuente de datos: API pública de Binance (gratis, sin API key, válida para
cripto). Para Forex/futuros habría que sustituir fetch_closed_candles() por
otra fuente (ver notas al final del archivo).
"""

import json
import os
from pathlib import Path

import requests

# ===================== CONFIGURACIÓN =====================
SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")          # par de Binance
EMA_LEN = int(os.environ.get("EMA_LEN", "50"))
NTFY_TOPIC = os.environ["NTFY_TOPIC"]                  # obligatorio (Secret)
MAX_WAIT_HOURS = float(os.environ.get("MAX_WAIT_HOURS", "6"))  # 0 = sin límite

STATE_FILE = Path("state.json")
BINANCE_URL = "https://api.binance.com/api/v3/klines"

DEFAULT_STATE = {
    "initialized": False,
    "searching": False,
    "search_start_ts": None,
    "state5_buy": 0,
    "state5_sell": 0,
    "state1_buy": 0,
    "state1_sell": 0,
    "last_h1_ts": 0,
    "last_m5_ts": 0,
    "last_m1_ts": 0,
}


# ===================== DATOS =====================
def fetch_closed_candles(interval, limit=300):
    """Velas YA CERRADAS de Binance (se descarta la última, que puede seguir abierta)."""
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_URL, params=params, timeout=15)
    r.raise_for_status()
    raw = r.json()
    closed = raw[:-1] if len(raw) > 1 else []
    return [
        {
            "open_time": c[0],
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        }
        for c in closed
    ]


def compute_ema(candles, length):
    closes = [c["close"] for c in candles]
    if not closes:
        return []
    k = 2 / (length + 1)
    out = [closes[0]]
    for price in closes[1:]:
        out.append(price * k + out[-1] * (1 - k))
    return out


# ===================== ESTADO PERSISTENTE =====================
def load_state():
    if STATE_FILE.exists():
        try:
            return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def reset_search(state, keep_searching=False):
    state["searching"] = keep_searching
    state["state5_buy"] = 0
    state["state5_sell"] = 0
    state["state1_buy"] = 0
    state["state1_sell"] = 0


# ===================== NOTIFICACIÓN =====================
def send_push(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "rotating_light"},
            timeout=10,
        )
    except Exception as e:
        print(f"Error enviando notificación: {e}")


# ===================== LÓGICA PRINCIPAL =====================
def main():
    state = load_state()

    h1 = fetch_closed_candles("1h")
    m5 = fetch_closed_candles("5m")
    m1 = fetch_closed_candles("1m")
    h1_ema = compute_ema(h1, EMA_LEN)
    m5_ema = compute_ema(m5, EMA_LEN)
    m1_ema = compute_ema(m1, EMA_LEN)

    # Primer arranque: solo fijamos el punto de partida, sin generar señales
    # con datos históricos antiguos.
    if not state["initialized"]:
        state["last_h1_ts"] = h1[-1]["open_time"] if h1 else 0
        state["last_m5_ts"] = m5[-1]["open_time"] if m5 else 0
        state["last_m1_ts"] = m1[-1]["open_time"] if m1 else 0
        state["initialized"] = True
        save_state(state)
        print("Inicializado. Esperando nuevas velas cerradas.")
        return

    # ---- 1) Backlog de 1H: detectar TOQUE de la EMA50 ----
    new_h1 = [(c, e) for c, e in zip(h1, h1_ema) if c["open_time"] > state["last_h1_ts"]]
    for candle, ema_val in new_h1:
        state["last_h1_ts"] = candle["open_time"]
        touch = candle["low"] <= ema_val <= candle["high"]
        if touch:
            reset_search(state, keep_searching=True)
            state["search_start_ts"] = candle["open_time"]

    # ---- Timeout de búsqueda ----
    if state["searching"] and MAX_WAIT_HOURS > 0 and state["search_start_ts"] and h1:
        elapsed_ms = h1[-1]["open_time"] - state["search_start_ts"]
        if elapsed_ms > MAX_WAIT_HOURS * 3600 * 1000:
            reset_search(state, keep_searching=False)

    # ---- 2) Backlog de 5M: ruptura + retest ----
    new_m5 = [(c, e) for c, e in zip(m5, m5_ema) if c["open_time"] > state["last_m5_ts"]]
    for candle, ema_val in new_m5:
        state["last_m5_ts"] = candle["open_time"]
        if not state["searching"]:
            continue
        close, high, low = candle["close"], candle["high"], candle["low"]

        if state["state5_buy"] == 0 and close > ema_val:
            state["state5_buy"] = 1
        elif state["state5_buy"] == 1:
            if close < ema_val:
                state["state5_buy"] = 0
            elif low <= ema_val and close > ema_val:
                state["state5_buy"] = 2

        if state["state5_sell"] == 0 and close < ema_val:
            state["state5_sell"] = 1
        elif state["state5_sell"] == 1:
            if close > ema_val:
                state["state5_sell"] = 0
            elif high >= ema_val and close < ema_val:
                state["state5_sell"] = 2

    # ---- 3) Backlog de 1M: confirmación final ----
    new_m1 = [(c, e) for c, e in zip(m1, m1_ema) if c["open_time"] > state["last_m1_ts"]]
    for candle, ema_val in new_m1:
        state["last_m1_ts"] = candle["open_time"]
        if not state["searching"]:
            continue
        close, high, low = candle["close"], candle["high"], candle["low"]

        if state["state5_buy"] >= 1:
            if state["state1_buy"] == 0 and close > ema_val:
                state["state1_buy"] = 1
            elif state["state1_buy"] == 1:
                if close < ema_val:
                    state["state1_buy"] = 0
                elif low <= ema_val and close > ema_val:
                    state["state1_buy"] = 2
        else:
            state["state1_buy"] = 0

        if state["state5_sell"] >= 1:
            if state["state1_sell"] == 0 and close < ema_val:
                state["state1_sell"] = 1
            elif state["state1_sell"] == 1:
                if close > ema_val:
                    state["state1_sell"] = 0
                elif high >= ema_val and close < ema_val:
                    state["state1_sell"] = 2
        else:
            state["state1_sell"] = 0

        if state["state5_buy"] == 2 and state["state1_buy"] == 2:
            send_push(
                "📈 BUY - EMA50 MTF",
                f"{SYMBOL}: confirmación 1H+5M+1M completada (COMPRA) cerca de {close}",
            )
            reset_search(state, keep_searching=False)
        elif state["state5_sell"] == 2 and state["state1_sell"] == 2:
            send_push(
                "📉 SELL - EMA50 MTF",
                f"{SYMBOL}: confirmación 1H+5M+1M completada (VENTA) cerca de {close}",
            )
            reset_search(state, keep_searching=False)

    save_state(state)
    print("OK ->", json.dumps(state))


if __name__ == "__main__":
    main()

# ===================== NOTAS =====================
# - Forex / futuros: Binance no cubre estos mercados. Sustituye
#   fetch_closed_candles() por otra fuente (p. ej. la API REST de OANDA,
#   gratis en cuentas demo, con buen límite de peticiones). Para futuros
#   no existe una fuente gratuita fiable de datos en tiempo real a 1 minuto;
#   ahí la opción de TradingView (Pine Script) sigue siendo la más práctica.
