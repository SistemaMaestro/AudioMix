# StudioLive 32SC — Hello World (UCNET / Ethernet)

Controle mínimo sobre a PreSonus StudioLive Série III via rede, com UI web.
Baseado no protocolo engenharia-reversa do projeto
[featherbear/presonus-studiolive-api](https://github.com/featherbear/presonus-studiolive-api).

## Rodar

```
pip install -r requirements.txt
python server.py
```

Abrir http://127.0.0.1:8000

## Fluxo

1. **Procurar mesa** — escuta UDP `47809` por broadcasts da mesa (a cada ~3s).
2. **Conectar** — abre TCP `53000`, envia `JM Subscribe`, inicia `KA` a cada 1s.
3. **Volume / Mute** — envia `PV` com path `line/ch1/volume` (float 0..1) ou `line/ch1/mute` (0/1).
4. **Raw PV** — envia qualquer parâmetro pelo path.

## Protocolo (resumo)

```
header      55 43 00 01            "UC\0\1"
payload_len uint16 LE              = 2 + 4 + len(data)
code        2 ASCII                "KA" "JM" "PV" "MS" "ZB" ...
cbytes      68 00 65 00            request/response pairing
data        ...                    payload específico do code
```

- `JM` subscribe: `len16LE + 00 00 + JSON bytes`
- `PV`: `"<path>\x00\x00\x00" + float32 LE`
- Boolean = float 0.0 / 1.0

## Paths úteis

| Path                       | Valor         |
|----------------------------|---------------|
| `line/chN/volume`          | 0.0..1.0      |
| `line/chN/mute`            | 0 / 1         |
| `line/chN/solo`            | 0 / 1         |
| `line/chN/pan` / `stereopan` | 0..1 (0=L, 1=R) |
| `main/ch1/volume`          | 0.0..1.0      |
| `aux/chN/volume`           | 0.0..1.0      |
| `filtergroup/chN/volume`   | 0.0..1.0 (DCA)|

Escala do fader: `0`=−84 dB, `72`≈unity (0 dB), `100`=+10 dB.

## Limitações conhecidas (para expandir depois)

- Não faz parsing dos pacotes `ZB` (estado inicial compactado com zlib + UBJSON) — só loga códigos recebidos.
- Não faz parsing dos `MS` (posições de fader / metering em tempo real).
- Send a aux mix usa path diferente de fader master; ver `setLevel` no Client.ts original.
- Só um cliente por vez.
