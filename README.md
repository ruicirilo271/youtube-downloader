# YT Forge Super Deus — Deno incorporado

Esta versão inclui o Deno Linux comprimido em `bin/deno-linux-x86_64.gz`.
A função extrai o binário para `/tmp/ytforge_runtime/deno` e usa-o no yt-dlp.

## Variáveis no Vercel

- `YOUTUBE_API_KEY`
- `YOUTUBE_COOKIES_B64`

## Diagnóstico

Abre `/api/health`. O resultado esperado inclui:

```json
"embedded_deno_archive": true,
"javascript_runtime": {
  "name": "deno",
  "source": "embedded-gzip"
}
```

## Limites padrão

Como o Deno extraído ocupa cerca de 103 MB de `/tmp`, o limite seguro da pasta de trabalho foi ajustado para 300 MB e o ficheiro final para 180 MB.
