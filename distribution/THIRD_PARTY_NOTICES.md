# Componentes incluídos no CompraHub

Esta distribuição inclui os componentes abaixo. Os avisos de licença disponíveis
são copiados para third_party_licenses dentro do executável e no ZIP de distribuição.

- Python e biblioteca padrão: Python Software Foundation License.
  https://docs.python.org/3/license.html
- Playwright e driver: Apache License 2.0.
  https://github.com/microsoft/playwright-python
- Chromium / Chrome for Testing: licenças do projeto Chromium e de seus componentes.
  Os recursos e avisos originais do navegador são preservados. Créditos podem ser
  consultados no próprio navegador em chrome://credits.
  https://www.chromium.org/chromium-os/licenses/
- Node.js, incluído no driver do Playwright: avisos presentes no pacote do driver.
  https://github.com/nodejs/node/blob/main/LICENSE
- greenlet, pyee e typing_extensions: licenças incluídas nos metadados dos pacotes.
- PyInstaller: GPL com exceção específica para distribuição de aplicações geradas.
  https://pyinstaller.org/en/stable/license.html

O diretório de perfis, os cookies, os cadastros e os históricos da máquina que
compilou o programa não fazem parte dos arquivos de distribuição.
