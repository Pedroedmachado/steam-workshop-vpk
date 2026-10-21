# Steam Workshop → VPK

Cole um link da Steam Workshop e baixe o conteúdo como `.vpk` no seu computador.

## Download

Baixe o `WorkshopVPK.exe` na página de [Releases](../../releases/latest). Não precisa instalar nada.

## Como usar

1. Abra o `WorkshopVPK.exe`.
2. Cole um ou mais links da Workshop, um por linha (links de coleção também funcionam).
3. Escolha a pasta de destino (padrão: `Downloads\Workshop VPK`) e clique em **Baixar .vpk**.

Também funciona pela linha de comando:

```
WorkshopVPK.exe "https://steamcommunity.com/sharedfiles/filedetails/?id=3819533403" -o "C:\minha\pasta"
```

## Como funciona

1. Consulta a Steam Web API (`GetPublishedFileDetails`), que não exige chave.
2. Se o item tiver link direto, baixa por ele.
3. Se não tiver link direto, ou se ele falhar, usa o **SteamCMD** com login anônimo. Na primeira vez o SteamCMD é baixado automaticamente para `%LOCALAPPDATA%\SteamWorkshopVPK`.
4. Se o item já for `.vpk`, salva como está. Se vier como pasta solta ou addon `.gma` (Garry's Mod), empacota tudo num `.vpk`.

## Limitações

- Alguns jogos não permitem baixar a Workshop sem login, só com o jogo na conta Steam. Nesses casos o download falha com uma mensagem explicando isso.
- O Garry's Mod não carrega `.vpk` como addon. O `.vpk` gerado a partir de um `.gma` serve para extrair o conteúdo ou para usar em outros jogos da engine Source.

## Rodar pelo código-fonte

```
pip install -r requirements.txt
python workshop_vpk.py
```

Para gerar o executável:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name WorkshopVPK workshop_vpk.py
```
