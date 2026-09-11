#!/usr/bin/env python3
"""Interactive entry of the limited RevenueCat customer-management key."""

import getpass
import os
import re
from pathlib import Path


def main():
    print("RevenueCat → projeto Queep → API keys → nova chave secreta V2.")
    print("Autorize somente Customer information / Customers: Read & Write.")
    key = getpass.getpass("Chave secreta V2 (não será exibida): ").strip()
    if not re.fullmatch(r"sk_[A-Za-z0-9]+", key):
        raise SystemExit("Formato de chave inválido.")
    path = Path(__file__).resolve().parents[1] / ".env.accounts"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(f"REVENUECAT_PROJECT_ID=projf6e36522\nREVENUECAT_DELETE_KEY={key}\n")
    print("Chave salva no arquivo local privado .env.accounts. Não cole a chave no chat.")
    print("Avise o Codex para validar e configurar a limpeza no servidor.")


if __name__ == "__main__":
    main()
