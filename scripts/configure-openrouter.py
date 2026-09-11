"""Store a project-scoped evaluation key without shell history or terminal echo."""

import getpass
import os
from pathlib import Path

target = Path(__file__).resolve().parents[1] / ".env.evaluation"
key = getpass.getpass("Chave OpenRouter do Queep (entrada oculta): ").strip()
if not key.startswith("sk-or-") or any(character.isspace() for character in key):
    raise SystemExit("Formato de chave inválido; nenhum arquivo alterado.")
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(descriptor, "w") as file:
    os.fchmod(file.fileno(), 0o600)
    file.write(f"OPENROUTER_API_KEY={key}\n")
print("Chave salva em .env.evaluation, fora do Git e acessível somente ao seu usuário.")
