"""
tools/ai_smoke.py - one real Gemini call through core/ai_client.py (T028).

Checks, end to end, that the key, the model, the prepaid balance and the
usage log all work: prints the answer, tokens and estimated cost, and the
company's AI calls today. Writes one usage entry (feature "smoke") to the
chosen company. Refuses any project other than regulator-dev.

Usage (PowerShell, backend venv active, from the regulator-backend folder):
    python tools\\ai_smoke.py
    python tools\\ai_smoke.py --tenant sustainer-demo --prompt "Reply with OK"
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default="test-tenant-001")
    p.add_argument("--prompt", default="In one sentence, what does the EU Batteries Regulation's battery passport require?")
    args = p.parse_args(argv)

    from core import ai_client
    from core.config import GCP_PROJECT

    if GCP_PROJECT != "regulator-dev":
        print(f"Refusing: this tool is for regulator-dev, but GCP_PROJECT is '{GCP_PROJECT}'.")
        return 2
    s = ai_client.settings()
    if not s["api_key"]:
        print("GEMINI_API_KEY is not set in .env.")
        return 2

    print(f"[ai] model {s['model']}, company '{args.tenant}', daily limit {s['daily_limit']}")
    try:
        r = ai_client.generate(args.tenant, "smoke", args.prompt, max_output_tokens=200, actor_uid="smoke-tool")
    except ai_client.AIError as e:
        print(f"[ai] FAILED ({e.code}): {e.message}")
        print(f"     detail: {e.detail}")
        return 1
    print(f"[ai] answer: {r.text}")
    print(f"[ai] tokens in {r.input_tokens}, out {r.output_tokens} (incl. thinking); "
          f"estimated cost USD {r.cost_usd:.6f}; finish {r.finish_reason}")
    print(f"[ai] calls today for '{args.tenant}': {ai_client.calls_today(args.tenant)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
