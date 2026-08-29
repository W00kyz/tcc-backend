# Contribuindo — tcc-backend

## Branches

| Prefixo | Uso |
|---|---|
| `feat/` | funcionalidade nova |
| `fix/` | correção de defeito |
| `chore/` | ferramental, dependências, CI |
| `docs/` | documentação |
| `refactor/` | mudança sem alteração de comportamento |

Nome em inglês, kebab-case, referenciando o requisito quando houver:
`feat/rf29-qr-check-in`.

`main` é protegida. Nada entra por push direto.

## Commits

[Conventional Commits](https://www.conventionalcommits.org). Tipos permitidos:
`feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, `revert`.

```
feat(execution): validate check-in radius against service point

Cross-references the QR floor with the GPS fix and rejects the scan when the
distance exceeds the configured tolerance. Records NOT_VALIDATED instead of
failing when the GPS fix is unavailable (RNF07).

Refs: RF32, RNF07
```

Assunto em inglês, imperativo, minúsculo, sem ponto final, até 72 caracteres. Corpo
explica **por que**. Rodapé cita os requisitos atendidos.

O hook `commit-msg` valida. **Nunca contornar com `--no-verify`** — hook que rejeita é
sinal de corrigir a mudança, não a validação.

**Sem trailer `Co-Authored-By`**, mesmo em commit assistido por agente. O autor em
`git config user.name` responde pela mudança na revisão.

## Pull requests

Título segue a convenção de commit. A descrição diz o que mudou, por que, como testar,
e quais requisitos atendeu. CI verde é obrigatório.

## Versões

Todas travadas. Imagens Docker por digest, GitHub Actions por SHA, dependências por
versão exata, arquivos de lock commitados. PR que introduz `latest`, `^` ou `~` é
rejeitado.

## Antes de abrir PR

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests && uv run pytest
```

Os quatro precisam passar. O `pre-commit` já roda isto no commit; este comando é a
conferência manual quando você quer olhar a saída inteira.

## Contrato OpenAPI (AD-04)

Toda mudança de request/response de rota exige reexportar o schema:

```bash
uv run python -m app.scripts.export_openapi
```

Isto escreve `../docs/api/openapi.json`, no repositório principal — commitar lá (não aqui),
depois rodar `npm run generate:api` no `tcc-dashboard` (ver `dashboard/CONTRIBUTING.md`) para
atualizar o cliente TypeScript gerado.
