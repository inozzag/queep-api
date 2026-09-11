#!/usr/bin/env python3
"""User-run handoff: copy the private header without printing it into chat/history."""
import subprocess
from pathlib import Path


def main():
    path = Path(__file__).resolve().parents[1] / ".env.billing"
    token = next((line.split("=", 1)[1] for line in path.read_text().splitlines()
                  if line.startswith("REVENUECAT_WEBHOOK_TOKEN=")), None)
    if not token or len(token) < 32:
        raise SystemExit("Configure primeiro o token exclusivo do Queep.")
    subprocess.run(["pbcopy"], input="Bearer " + token, text=True, check=True)
    print("Header copiado. Cole em Authorization header value no RevenueCat e clique Add webhook.")
    print("Não cole o conteúdo no chat.")


if __name__ == "__main__":
    main()
