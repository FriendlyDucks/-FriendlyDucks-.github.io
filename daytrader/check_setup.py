"""Kontrollerar att AI-motorn (Ollama, Claude Code eller Claude API) och Nordnet fungerar.

    python check_setup.py
"""

from __future__ import annotations

import sys

import config
from agents import MODEL


def check_ai() -> bool:
    kind = config.env("LLM_BACKEND", "ollama")
    print(f"1. AI-motor: {kind}")
    if kind == "api":
        return check_claude_api()
    from llm import DEFAULT_OLLAMA_MODEL, LLMError, make_backend

    try:
        backend = make_backend(kind, config.env("CLAUDE_MODEL", MODEL), "low",
                               config.env("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
                               config.env("OLLAMA_URL", "http://localhost:11434"))
        text = backend.complete("Du är en testagent.", "Svara bara: anslutningen fungerar")
        print(f"   OK – {backend.name} svarade: {text[:80]!r}")
        return True
    except (LLMError, OSError) as e:
        print(f"   FEL: {e}")
    except Exception as e:
        print(f"   FEL: {type(e).__name__}: {e}")
    return False


def check_claude_api() -> bool:
    import anthropic

    model = config.env("CLAUDE_MODEL", MODEL)
    try:
        client = anthropic.Anthropic()
        r = client.messages.create(
            model=model,
            max_tokens=2000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Svara bara: anslutningen fungerar"}],
        )
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        print(f"   OK – {model} svarade: {text!r}")
        print(f"   Tokens: {r.usage.input_tokens} in / {r.usage.output_tokens} ut")
        return True
    except anthropic.AuthenticationError:
        print("   FEL: ogiltig eller saknad ANTHROPIC_API_KEY (se .env.example)")
    except anthropic.PermissionDeniedError:
        print("   FEL: nyckeln saknar behörighet – kontrollera kontot på platform.claude.com")
    except anthropic.NotFoundError:
        print(f"   FEL: modellen {model} finns inte för ditt konto")
    except anthropic.RateLimitError:
        print("   FEL: för många anrop eller slut på krediter – kontrollera Billing")
    except anthropic.APIStatusError as e:
        print(f"   FEL: {e.status_code} {e.message}")
    except anthropic.APIConnectionError:
        print("   FEL: kunde inte nå api.anthropic.com – kontrollera nätverket")
    return False


def check_data() -> bool:
    print("\n2. Kursdata")
    from market import EODHDMarketData, TwelveDataMarketData
    from orders import Tradable

    found = False
    for env_key, cls, symbol in (("TWELVEDATA_API_KEY", TwelveDataMarketData, "INVE B"),
                                 ("EODHD_API_KEY", EODHDMarketData, "VOLV B")):
        key = config.env(env_key)
        if not key:
            continue
        found = True
        t = Tradable(symbol=symbol, market_id=0, identifier=symbol)
        try:
            bars, ref, last = cls(key)._fetch(t)
            price = last or float(bars["Close"].iloc[-1])
            print(f"   OK – {cls.name}: {symbol} senast {price:.2f} ({len(bars)} minutstaplar idag, igår {ref:.2f})")
        except Exception as e:
            print(f"   FEL – {cls.name}: {e}")
            if cls is TwelveDataMarketData:
                print("   Tips: gratisnyckeln täcker bara testaktien INVE B. Övriga Stockholmsaktier kräver Pro-planen.")
            return False
    if not found and not config.nordnet_configured():
        print("   FEL: ingen datakälla. Ange TWELVEDATA_API_KEY eller EODHD_API_KEY i .env (se README).")
        return False
    return True


def check_nordnet() -> bool:
    print("\n3. Nordnet API")
    if not config.nordnet_configured():
        print("   Hoppar över: NORDNET_API_KEY / NORDNET_PRIVATE_KEY saknas (Nordnet tar inte in nya API-kunder just nu).")
        return True
    client = config.make_nordnet_client()
    try:
        info = client.login()
        print(f"   OK – inloggad. Publik feed: {info['public_feed']['hostname']}:{info['public_feed']['port']}")
        for acc in client.accounts():
            print(f"   Konto accid={acc.get('accid')} nr={acc.get('accno')} typ={acc.get('type')} "
                  f"förvalt={acc.get('default', acc.get('is_default'))}")
        t = client.tradable_from_symbol("VOLV B", 11)
        price = client.tradable_info(t).get("price_info", {})
        print(f"   Testkurs VOLV B ({t.key}): senast {price.get('last')}, bid {price.get('bid')}, ask {price.get('ask')}")
        return True
    except Exception as e:
        print(f"   FEL: {e}")
        return False
    finally:
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    config.load_env()
    ok = check_ai() & check_data() & check_nordnet()
    print("\nAllt klart!" if ok else "\nÅtgärda felen ovan och kör igen.")
    sys.exit(0 if ok else 1)
