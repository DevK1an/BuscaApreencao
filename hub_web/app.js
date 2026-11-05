"use strict";

const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="hub-token"]').content;
const formatMoney = (cents) => new Intl.NumberFormat("pt-BR", {style:"currency", currency:"BRL"}).format(cents / 100);
const escapeHtml = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const icon = (name) => '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
const labels = {
  idle:"Pronto para iniciar", running:"Iniciando", monitoring:"Monitorando",
  preparing:"Preparando pedido", needs_attention:"Precisa de você",
  ready:"Sua confirmação", opened:"Produto aberto", confirmed:"Confirmado na loja", error:"Verificar atividade",
  stopping:"Parando", stopped:"Parado", finished:"Encerrado",
};
let state = {products:[], active_id:null};
let filter = "all";
let editingId = null;
let activityId = null;
let toastTimer;
let signature = null;
let busy = false;
let refreshing = false;
let mutationVersion = 0;

function toast(message, error=false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").className = "toast" + (error ? " error" : "");
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, error ? 6500 : 3500);
}

async function api(path, method="GET", body) {
  const response = await fetch(path, {
    method,
    headers: {"X-Hub-Token":token, ...(body ? {"Content-Type":"application/json"} : {})},
    ...(body ? {body:JSON.stringify(body)} : {}),
    cache:"no-store",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Não foi possível concluir esta operação.");
  return data;
}

function applyState(data) {
  state = data;
  render();
}

function render() {
  const products = state.products;
  const active = products.find((p) => p.active);
  const ready = products.filter((p) => p.active && p.status.state === "ready").length;
  $("#stat-total").textContent = products.length;
  $("#stat-active").innerHTML = products.filter((p) => p.active).length + "<span> / 1</span>";
  $("#stat-ready").textContent = ready;
  $("#product-count").textContent = products.length;
  $("#nav-count").textContent = products.length;
  const panel = $("#execution");
  panel.className = "execution" + (active ? " " + active.status.state : "");
  $("#execution-title").textContent = active ? active.name + " · " + active.status.title : "Tudo pronto para começar";
  $("#execution-detail").textContent = active ? active.status.detail : "Escolha um produto abaixo e clique em Iniciar. O navegador abre automaticamente.";
  $("#execution-actions").innerHTML = active
    ? '<button class="button secondary" data-action="activity" data-id="' + active.id + '">' + icon("activity") + 'Ver atividade</button>'
      + '<button class="button danger-soft" data-action="stop" data-id="' + active.id + '"' + (active.status.state === "stopping" ? " disabled" : "") + '>' + icon("stop") + 'Parar</button>'
    : "";
  renderProducts();
}

function renderProducts(force=false) {
  const query = $("#search").value.trim().toLowerCase();
  const products = state.products.filter((p) => {
    const matchesQuery = (p.name + " " + p.asin).toLowerCase().includes(query);
    const matchesFilter = filter === "all" || (filter === "active" && p.active) || (filter === "ready" && p.active && p.status.state === "ready");
    return matchesQuery && matchesFilter;
  });
  const nextSignature = JSON.stringify([products, state.active_id, filter, query, busy]);
  if (!force && (signature === nextSignature || document.activeElement?.matches("[data-quantity]"))) return;
  signature = nextSignature;
  const list = $("#product-list");
  list.innerHTML = products.map((p) => {
    const otherBusy = state.active_id && state.active_id !== p.id;
    const locked = p.active || busy;
    const maxTotal = p.max_unit_cents * p.quantity;
    const startDisabled = busy || otherBusy || p.status.state === "stopping";
    const name = escapeHtml(p.name);
    const shopLabel = p.mode === "search" ? "GOOGLE SHOPPING" : p.marketplace === "mercadolivre" ? "MERCADO LIVRE" : p.marketplace === "external" ? "LOJA EXTERNA" : "AMAZON";
    return '<article class="product-row" data-product="' + p.id + '">'
      + '<div class="product-info"><span class="product-visual">' + icon("box") + '</span><div class="product-text">'
      + '<a class="product-name" href="' + escapeHtml(p.url) + '" target="_blank" rel="noopener noreferrer" title="' + name + '">' + name + icon("arrow") + '</a>'
      + '<p class="product-code"><span>' + shopLabel + '</span>' + escapeHtml(p.mode === 'search' ? (p.pages_per_store + " rodadas · " + (p.excluded_stores?.length || 0) + " lojas excluídas") : p.asin) + '</p></div></div>'
      + '<div class="quantity-cell" data-label="QUANTIDADE"><div class="quantity-control">'
      + '<button data-action="minus" data-id="' + p.id + '" aria-label="Diminuir quantidade de ' + name + '"' + (locked || p.quantity <= 1 ? " disabled" : "") + '>−</button>'
      + '<input type="number" min="1" max="999" step="1" value="' + p.quantity + '" data-quantity="' + p.id + '" aria-label="Quantidade de ' + name + '"' + (locked ? " disabled" : "") + '>'
      + '<button data-action="plus" data-id="' + p.id + '" aria-label="Aumentar quantidade de ' + name + '"' + (locked || p.quantity >= 999 ? " disabled" : "") + '>+</button></div></div>'
      + '<div class="price-cell" data-label="LIMITE / UNIDADE"><strong>' + formatMoney(p.max_unit_cents) + '</strong><small>' + (p.quantity > 1 ? formatMoney(maxTotal) + " teto do pedido" : "Frete incluído no teto") + '</small><button class="history-link" data-action="prices" data-id="' + p.id + '">Ofertas e histórico ' + icon("activity") + '</button></div>'
      + '<div class="badge-cell"><button class="badge ' + p.status.state + '" data-action="activity" data-id="' + p.id + '" title="Ver atividade deste produto">' + (labels[p.status.state] || "Aguardando") + '</button></div>'
      + '<div class="row-actions"><button class="button run-button' + (p.active ? " stop" : "") + '" data-action="' + (p.active ? "stop" : "start") + '" data-id="' + p.id + '"' + (startDisabled ? " disabled" : "") + ' title="' + (otherBusy ? "Pare o produto atual antes de iniciar outro" : p.active ? "Parar monitoramento" : "Abrir produto e iniciar monitoramento") + '">'
      + icon(p.active ? "stop" : "play") + (p.active ? p.status.state === "stopping" ? "Parando" : "Parar" : "Iniciar") + '</button>'
      + '<button class="icon-button" data-action="edit" data-id="' + p.id + '" aria-label="Editar ' + name + '" title="Editar produto"' + (locked ? " disabled" : "") + '>' + icon("edit") + '</button>'
      + '<button class="icon-button" data-action="delete" data-id="' + p.id + '" aria-label="Excluir ' + name + '" title="Excluir produto"' + (locked ? " disabled" : "") + '>' + icon("trash") + '</button></div></article>';
  }).join("");
  $("#empty-state").hidden = products.length > 0;
  const empty = $("#empty-state");
  empty.querySelector("h3").textContent = state.products.length ? "Nenhum produto por aqui" : "Sua próxima compra começa aqui";
  empty.querySelector("p").textContent = state.products.length ? "Experimente outro filtro ou termo de busca." : "Use Rastrear preços para buscar no Google Shopping.";
  $("#empty-add").hidden = state.products.length > 0;
}

function openForm(product=null) {
  editingId = product?.id || null;
  $("#product-form").reset();
  $("#product-mode").value = product?.mode || "url";
  $("#product-query").value = product?.query || "";
  $("#product-pages").value = product?.pages_per_store || 30;
  $("#product-excluded").value = (product?.excluded_stores ?? ["AliExpress","Shopee"]).join("\n");
  $("#product-partial").checked = product?.allow_partial !== false;
  $("#form-title").textContent = product ? "Editar produto" : "Adicionar produto";
  $("#save-product").innerHTML = (product ? "Salvar alterações" : "Salvar produto") + icon("check");
  $("#product-url").value = product?.url || "";
  if (window.updateTrackingMode) window.updateTrackingMode();
  $("#product-name").value = product?.name || "";
  $("#product-quantity").value = product?.quantity || 1;
  $("#product-price").value = new Intl.NumberFormat("pt-BR", {minimumFractionDigits:2, maximumFractionDigits:2}).format((product?.max_unit_cents || 450000) / 100);
  const interval = product?.interval_seconds || 120;
  const select = $("#product-interval");
  select.querySelectorAll("[data-custom]").forEach((option) => option.remove());
  if (![...select.options].some((o) => Number(o.value) === interval)) {
    const option = document.createElement("option");
    option.value = interval;
    option.textContent = "A cada " + interval + " segundos";
    option.dataset.custom = "true";
    select.append(option);
  }
  select.value = interval;
  $("#form-error").hidden = true;
  updateBudget();
  $("#product-dialog").showModal();
  ($("#product-mode").value === "search" ? $("#product-query") : $("#product-url")).focus();
}

function updateBudget() {
  let raw = $("#product-price").value.replace(/R\$|\s/g,"");
  if (raw.includes(",")) raw = raw.replace(/\./g,"").replace(",",".");
  const amount = Number(raw), quantity = Number($("#product-quantity").value);
  $("#budget-preview").textContent = Number.isFinite(amount) && amount > 0 && Number.isInteger(quantity) && quantity > 0
    ? formatMoney(Math.round(amount * 100) * quantity) : "—";
}

function productPayload(product, quantity=product.quantity) {
  return {mode:product.mode || "url", search_provider:"google_shopping", excluded_stores:product.excluded_stores ?? ["AliExpress","Shopee"], query:product.query, marketplaces:product.marketplaces, pages_per_store:product.pages_per_store, allow_partial:product.allow_partial !== false, name:product.name, url:product.url, quantity, max_price:(product.max_unit_cents / 100).toFixed(2), interval_seconds:product.interval_seconds};
}

async function changeQuantity(product, quantity) {
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 999) throw new Error("Escolha uma quantidade de 1 a 999.");
  applyState(await api("/api/products/" + product.id, "PUT", productPayload(product, quantity)));
  toast("Quantidade salva.");
}

async function refreshLogs() {
  if (!activityId || !$("#activity-dialog").open) return;
  const data = await api("/api/products/" + activityId + "/logs");
  const pre = $("#activity-log");
  const wasAtBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 50;
  const text = data.logs || "Nenhuma atividade ainda. Clique em Iniciar para acompanhar este produto.";
  if (pre.textContent !== text) {
    pre.textContent = text;
    if (wasAtBottom) pre.scrollTop = pre.scrollHeight;
  }
}

document.addEventListener("click", async (event) => {
  const close = event.target.closest("[data-close]");
  if (close) { close.closest("dialog").close(); return; }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled || busy) return;
  const product = state.products.find((p) => p.id === button.dataset.id);
  if (!product) return;
  const action = button.dataset.action;
  if (action === "prices") { window.openPriceHistory(product); return; }
  if (action === "edit") { openForm(product); return; }
  if (action === "activity") {
    activityId = product.id;
    $("#activity-product").textContent = product.name;
    $("#activity-dialog").showModal();
    try { await refreshLogs(); } catch (error) { toast(error.message, true); }
    return;
  }
  mutationVersion += 1;
  busy = true; renderProducts(true);
  try {
    if (action === "plus" || action === "minus") {
      await changeQuantity(product, product.quantity + (action === "plus" ? 1 : -1));
    } else if (action === "start" || action === "stop") {
      applyState(await api("/api/products/" + product.id + "/" + action, "POST"));
      toast(action === "start" ? "Iniciando a consulta no navegador do bot." : "Parada solicitada.");
    } else if (action === "delete") {
      applyState(await api("/api/products/" + product.id, "DELETE"));
      toast("Produto excluído.");
    }
  } catch (error) { toast(error.message, true); }
  finally { mutationVersion += 1; busy = false; renderProducts(true); }
});

$("#product-list").addEventListener("change", async (event) => {
  const id = event.target.dataset.quantity;
  if (!id || busy) return;
  const product = state.products.find((p) => p.id === id);
  if (!product) return;
  mutationVersion += 1;
  busy = true;
  try { await changeQuantity(product, Number(event.target.value)); }
  catch (error) { toast(error.message, true); }
  finally { mutationVersion += 1; busy = false; renderProducts(true); }
});

$("#product-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#save-product");
  mutationVersion += 1;
  busy = true;
  button.disabled = true;
  $("#form-error").hidden = true;
  const form = new FormData(event.currentTarget);
  const data = {
    mode:form.get("mode"), search_provider:"google_shopping", excluded_stores:String(form.get("excluded_stores") || "").split(/[\n;,]+/).map(s=>s.trim()).filter(Boolean), query:form.get("query"), pages_per_store:Number(form.get("pages_per_store")), allow_partial:$("#product-partial").checked,
    name:form.get("name"), url:form.get("url"),
    quantity:Number(form.get("quantity")), max_price:form.get("max_price"),
    interval_seconds:Number(form.get("interval_seconds")),
  };
  try {
    const wasEditing = Boolean(editingId);
    applyState(await api(editingId ? "/api/products/" + editingId : "/api/products", wasEditing ? "PUT" : "POST", data));
    $("#product-dialog").close();
    toast(wasEditing ? "Produto atualizado." : "Produto cadastrado. Você já pode iniciar.");
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  } finally { mutationVersion += 1; button.disabled = false; busy = false; renderProducts(true); }
});

$("#add-product").addEventListener("click", () => openForm());
$("#empty-add").addEventListener("click", () => openForm());
$("#help-open").addEventListener("click", () => $("#help-dialog").showModal());
$("#nav-products").addEventListener("click", () => {
  filter = "all"; $("#search").value = "";
  document.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("active", b.dataset.filter === filter));
  renderProducts();
});
$("#search").addEventListener("input", () => renderProducts());
$("#product-price").addEventListener("input", updateBudget);
$("#product-quantity").addEventListener("input", updateBudget);
document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  filter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("active", b === button));
  renderProducts();
}));
$("#activity-dialog").addEventListener("close", () => { activityId = null; });

async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    const version = mutationVersion;
    const snapshot = await api("/api/state");
    if (version === mutationVersion && !busy) applyState(snapshot);
    await refreshLogs();
    if (window.refreshPriceHistory) await window.refreshPriceHistory();
    document.querySelector(".privacy-chip").lastChild.textContent = "Hub local";
  } catch (error) {
    $("#execution-title").textContent = "O hub está desconectado";
    $("#execution-detail").textContent = "Mantenha a janela do terminal aberta ou execute abrir_hub.cmd novamente.";
  } finally { refreshing = false; }
}
refresh();
setInterval(refresh, 2000);
