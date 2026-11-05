# Distribuir o CompraHub no GitHub

O pacote foi preparado para Windows 11 x64. O outro computador não precisa de Python,
Playwright ou Chrome instalados: o executável inclui os recursos necessários.
Cada computador começa com seus próprios cadastros e faz seu próprio login na Amazon.

## Arquivos para enviar

Depois de compilar, a pasta dist contém:

- CompraHub.exe: aplicativo em arquivo único.
- CompraHub-windows-x64.zip: aplicativo, instruções e avisos de terceiros.
- SHA256SUMS.txt: hashes SHA-256 do executável e do ZIP.
- VERIFICACAO.json: resultado do diagnóstico offline do pacote.

Compartilhe o ZIP ou o executável. Não envie .hub, .amazon-profile ou a pasta de dados
do aplicativo em %LOCALAPPDATA%\CompraHub.

## Publicar os arquivos já gerados

1. Envie o código-fonte para o seu repositório, respeitando o .gitignore.
2. Abra Releases no GitHub e crie uma nova release, por exemplo v1.2.0.
3. Anexe CompraHub.exe, CompraHub-windows-x64.zip e SHA256SUMS.txt.
4. Publique a release. Os outros computadores baixam pela lista Assets.

Use os anexos de Releases para os binários; não faça commit do executável na árvore
de código. A pasta dist está no .gitignore.
Documentação: https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases

## Gerar novas versões no GitHub

O workflow .github/workflows/windows-release.yml está pronto:

- Em Actions, execute Gerar CompraHub para Windows para obter um artefato de teste.
- Uma tag começando com v, como v1.2.0, dispara testes, compilação, diagnóstico isolado
  e publicação dos arquivos na Release da própria tag.
- O workflow utiliza o GITHUB_TOKEN fornecido pelo GitHub, sem precisar salvar token
  pessoal no código. O repositório precisa permitir Actions e escrita de releases.
- Atualize a versão em app_paths.py e distribution/version_info.txt nas próximas versões.

O workflow só executa compras simuladas e não precisa de uma conta Amazon.
O envio de uma tag é a ação que inicia a publicação automática.

## Gerar localmente

Com as dependências do projeto preparadas, execute gerar_exe.cmd.
Para compilar manualmente:

    .venv\Scripts\python.exe -m pip install -r requirements-build.txt
    .venv\Scripts\python.exe -X utf8 build_exe.py

A compilação verifica o executável copiado para uma pasta temporária, com espaços e
acentos, sem Python no PATH nem acesso aos arquivos .py do projeto. O diagnóstico testa
o hub, o processo interno do bot, o navegador incluído e o checkout simulado.

A validação local não substitui testar o login e o checkout real em cada computador.
