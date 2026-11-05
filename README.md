# CompraHub — Google Shopping

O hub busca produtos por nome no Google Shopping e seleciona o menor preço anunciado
entre as ofertas compatíveis e permitidas que conseguiu consultar.

- Na Amazon Brasil, confere o produto e o preço atual, tenta ativar cupons, recusa
  seguro/garantia estendida reconhecidos e prepara o checkout. O último clique da
  compra continua sendo seu.
- Nas outras lojas, inclusive Mercado Livre, somente abre a página do produto.
  Não muda quantidade, aplica cupom nem adiciona itens ao carrinho dessas lojas.
- AliExpress e Shopee vêm excluídas por padrão. Em Lojas excluídas, adicione ou remova
  nomes ou domínios, um por linha. A seleção fica salva em cada cadastro.
- Quantidade, teto por unidade com frete e intervalo continuam configuráveis.

## Criar uma busca

Abra iniciar.cmd e clique em Rastrear preços. Informe marca, modelo, capacidade e
características do produto. Exemplo: Lápis BIC Evolution 12 cores.

Edite Lojas excluídas, escolha a quantidade, o teto e o intervalo. Use de 1 a 50 rodadas
de varredura; o padrão amplo é 30. O bot lê os cartões, abre seus detalhes, expande
Mais lojas, segue a paginação e rola para carregar mais resultados. A busca termina
quando os resultados carregáveis se esgotam, ocorre bloqueio/erro ou chega ao limite.

A cobertura aparece em Ofertas e histórico: cartões, produtos expandidos, ofertas,
exclusões e leituras incompletas. Quando permitida, uma seleção com cobertura parcial
fica identificada como tal. Desmarque a opção de usar ofertas parciais para exigir
uma varredura sem pendências.

Não há garantia de consultar todo o Google Shopping, todo o mercado ou todos os
vendedores. Resultados e preços variam por localização, sessão e disponibilidade.
A comparação inicial usa preços anunciados em reais; não presume frete grátis nem
estima descontos, impostos ou condições de parcelamento. O preço final é conferido
na loja. [Funcionamento do Google Shopping](https://support.google.com/googleshopping/answer/9128904?hl=pt-BR).

Se o Google pedir CAPTCHA, verificação de acesso ou consentimento, conclua pessoalmente
na janela. O programa aguarda e continua depois; não contorna bloqueios e não exige
API paga ou chave de serviço.

Cadastros antigos de busca por nome no hub passam ao Google Shopping ao reabrir o hub,
preservando identificador, nome, quantidade, teto e intervalo. O histórico pode mostrar
consultas anteriores de outras fontes. Alterar a identidade da busca/exclusões reinicia
o histórico desse cadastro.

## Exclusões e links

Aceita nomes (Shopee, AliExpress, Loja Azul) e domínios (shopee.com.br,
aliexpress.com, lojaazul.com.br). Excluir um domínio também exclui seus subdomínios.
A loja é filtrada pelo nome informado no Google e novamente pelo endereço resolvido.
Redirecionamentos automáticos para lojas excluídas são bloqueados antes de carregar
a página do produto.

O modo Monitorar um link aceita produtos Amazon, Mercado Livre e links HTTPS de
outras lojas públicas. Links Amazon precisam identificar o ASIN. Endereços locais,
IPs, credenciais no link e esquemas como file: não são aceitos. Em links externos
apenas abre a página; o teto informado não é apresentado como se fosse preço lido.

## Preparo na Amazon

A Amazon deve estar conectada na janela do bot. A sessão é reaproveitada neste
computador. Um carrinho com outros itens ou outra quantidade interrompe o preparo;
o bot não remove seus itens.

Depois de clicar Adicionar uma vez, mantém a página aberta até confirmar o produto
e a quantidade no carrinho, em aba auxiliar, por até 15 segundos. Se aparecer seguro
ou garantia estendida reconhecidos, seleciona Não, obrigado e não aceita Incluir proteção.

Tenta selecionar/resgatar cupons exibidos nos layouts reconhecidos. Um código externo
não é inventado: você pode digitá-lo na loja. Cupons já selecionados são preservados.
A resposta de ativação pode levar até oito segundos; não repete o clique durante a
espera. Após ativar no checkout, exige duas leituras iguais do total antes do aviso
de pronto. O total com frete precisa respeitar teto por unidade multiplicado pela
quantidade. Endereço, pagamento ou layouts desconhecidos podem exigir sua interação.

A verificação pública do seguro usou uma sessão sem login e confirmou uma unidade
no carrinho, sem iniciar checkout. As confirmações finais de compra e pagamento
são testadas apenas em páginas simuladas.

## Executável para outro computador

Use dist/CompraHub.exe ou extraia dist/CompraHub-windows-x64.zip. Python e Chromium
já estão incluídos. O destino validado é Windows 11 x64; a primeira abertura pode
demorar enquanto o navegador é extraído. Reserve aproximadamente 2 GB livres.

Cadastros, histórico e sessões do executável ficam em %LOCALAPPDATA%\\CompraHub.
O pacote distribuído não inclui contas, cookies ou cadastros pessoais. Atualizar o
executável mantém os dados desse computador. Não compartilhe essa pasta de dados.

Abra o .exe, cadastre seus produtos e clique em Iniciar. Mantenha o console aberto.
Há uma execução por vez. Use Parar antes de iniciar outra busca ou de ajustar um
carrinho que teve inclusão não confirmada. Ctrl+C encerra o hub e seu processo.

## Desenvolvimento local

Instale Python 3 de 64 bits e execute instalar.cmd, depois iniciar.cmd.
Os scripts também podem usar a distribuição Python local de desenvolvimento.

Para testes: .venv\\Scripts\\python.exe -X utf8 -m unittest discover -s tests -v

Para compilar: .venv\\Scripts\\python.exe -X utf8 build_exe.py

O build executa testes, coleta licenças, gera o arquivo único, verifica uma cópia
isolada sem Python externo e cria ZIP, SHA256SUMS.txt e VERIFICACAO.json.
PUBLICAR_NO_GITHUB.md descreve a publicação manual e o workflow de release.

CompraHub.exe --self-test --report diagnostico.json executa diagnóstico com páginas
simuladas, sem acessar contas ou confirmar compras.

## Dados locais

Na execução pelo código, os dados ficam no projeto: .hub/products.json,
.hub/prices.sqlite3 e .amazon-profile. Não envie essas pastas a terceiros.
O histórico exibe as últimas 100 varreduras por cadastro. Identificadores e valores
monetários são validados; preços são tratados em centavos inteiros.

Configurações antigas do CLI com search_provider=marketplaces são mantidas apenas
para compatibilidade. O formulário do hub cria buscas search_provider=google_shopping.
Mesmo no caminho legado, lojas fora da Amazon são apenas abertas.
