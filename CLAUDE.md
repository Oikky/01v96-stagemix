# 01V96 StageMix

Controle da mesa Yamaha 01V96 pelo celular/tablet com o visual do Yamaha TF StageMix, sem REAPER.
O PC fica ligado na mesa por USB-MIDI; o celular abre a página pelo Wi-Fi (ou pelo Tailscale, de longe).

    celular/tablet --Wi-Fi--> PC (INICIAR.bat, porta 8096) --USB--> 01V96

## Quem é o usuário e como trabalhar
- Técnico de som de igreja (mixa numa Yamaha TF5 e usa REAPER). Não é programador: explicar em linguagem simples.
- Responder sempre em português do Brasil, tom direto e informal.
- Decisões de produto (o que aparece na tela, fluxos) são dele: perguntar antes.
- A interface tem que ser cópia fiel do TF StageMix, peça por peça.
- Commits curtos em pt-BR no formato `Área: o que mudou` (ex.: `Ponte: medidor de GR do compressor`).
- Windows 11, PowerShell. Testar a página em celular e desktop antes de dizer que está pronto.

## Como funciona
- `servidor.py` imita a API web do REAPER + a ponte `igreja_remote` (projeto irmão, ver abaixo) e traduz tudo para SysEx da 01V96.
  Por isso `stagemix.html` é praticamente a mesma página da versão REAPER, com poucos ajustes:
  AUX 1-8 = tracks 33-40 como destinos de envio, SCENE no lugar do transporte, sem Width.
- EQ/GATE/COMP da mesa viram "plugins" falsos no protocolo de chaves `c<N>` (plugins, 10/s) e `m<N>` (medidores, rápido).
- `yamaha01v96.py`: mapa de parâmetros, tirado da planilha oficial "01V96 V2 Parameter Change List"
  (cópia em `referencia\`, vinda do projeto kryops/remote-mixer, MIT).
- `midi_win.py`: MIDI do Windows (winmm). `tabelas_01v96.json`: tabelas de fader, Q, frequência, ratio, tempos.
- Python portátil em `python\` (não precisa instalar). Driver Yamaha USB-MIDI 3.1.5 em `drivers\`
  (instalar `drivers\YamahaUSBMIDI\um3151\setup.exe` ANTES de ligar o cabo).
- Rodar: `INICIAR.bat` (mesa real), `INICIAR (simulador sem mesa).bat` (`--simular`), `LISTAR PORTAS MIDI.bat`.
  Opções: `--midi NUMERO`, `--fader-low`. Log em `servidor.log`. Valores crus da mesa em `http://IP:8096/diag`.
- Passo a passo de configuração da mesa e solução de problemas: `LEIA-ME.txt`.

## Estado (2026-10-07)
Tudo funcionou só no simulador. AINDA NÃO TESTADO NA MESA REAL. Conferir na mesa:
1. Nome da porta MIDI (o servidor procura a 01V96 sozinho; se não achar, `--midi NUMERO`).
2. Fader Resolution: HIGH = 0-1023 (padrão do servidor); `--fader-low` = 0-255 (o que os projetos de referência usavam).
3. Formato do medidor de GR do gate/comp: o palpite é "ganho com 0 dB = sem redução". Conferir em `/diag`.
4. Se a 01V96 aceita vários pedidos de medidor ao mesmo tempo.
5. Troca de cena (Program Change Tx ON) fazendo a página reler tudo.

Limites da mesa: sem RTA (a 01V96 não manda áudio), sem ganho analógico/phantom por MIDI.
Ainda não implementado: efeitos internos (REV/DELAY da mesa).

## Projeto irmão (só existe no PC do REAPER)
A página original fica em `C:\REAPER SITE\stagemix.html` (controla o REAPER pela interface web, porta 8080,
com a ponte Lua `igreja_remote.lua`). Melhorias feitas lá podem ser portadas pra cá reaplicando as trocas de texto
que diferenciam as duas versões (AUX/SCENE/Width). Não está neste repositório.
