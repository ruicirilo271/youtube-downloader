# YT Forge Super Deus — Health Fix

Correções desta versão:

- remove a referência inexistente `find_deno_bin` que provocava erro 500 em `/api/health`;
- move o arquivo Deno para `api/bin/deno-linux-x86_64.gz`;
- usa `API_DIR / "bin"` para localizar o runtime;
- torna `/api/health` resistente a exceções;
- remove o bloco `functions` do `vercel.json`.

Depois de publicar, abre `/api/health`.
