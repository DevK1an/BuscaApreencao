"use strict";
(() => {
  const Q = selector => document.querySelector(selector);
  let productId = null;
  let lastSignature = "";
  const storeName = key => ({amazon:"Amazon",mercadolivre:"Mercado Livre",google_shopping:"Google Shopping",external:"Loja externa"}[key] || key);
  const coverageName = {ok:"Consultado",empty:"Sem resultados",blocked:"Acesso bloqueado",unsupported:"Layout desconhecido",incomplete:"Leitura incompleta",error:"Falha de consulta"};
  const time = value => new Date(value * 1000).toLocaleString("pt-BR",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"});

  window.updateTrackingMode = () => {
    const searching = Q("#product-mode").value === "search";
    Q("#url-fields").hidden = searching;
    Q("#query-fields").hidden = !searching;
    Q("#product-url").required = !searching;
    Q("#product-query").required = searching;
    let linkStore = null;
    if (!searching) {
      const raw = Q("#product-url").value.trim();
      try {
        const url = new URL(raw.startsWith("https://") ? raw : "https://" + raw);
        if (["amazon.com.br","www.amazon.com.br"].includes(url.hostname)) linkStore = "amazon";
        if (["mercadolivre.com.br","www.mercadolivre.com.br","produto.mercadolivre.com.br"].includes(url.hostname)) linkStore = "mercadolivre";
      } catch {}
    }
    Q("#product-stores").hidden = !searching;
    Q("#stores-hint").textContent = searching
      ? "Compara lojas encontradas no Google Shopping, respeitando suas exclusões."
      : (linkStore === "amazon" ? "Amazon: prepara o carrinho e aguarda sua confirmação." : "Outras lojas: somente abre o produto, sem alterar o carrinho.");
  };
  Q("#product-mode").addEventListener("change", window.updateTrackingMode);
  Q("#product-url").addEventListener("input", window.updateTrackingMode);
  Q("#add-search").addEventListener("click", () => {
    openForm();
    Q("#product-mode").value = "search";
    window.updateTrackingMode();
    Q("#product-query").focus();
  });

  function drawChart(series) {
    const svg = Q("#price-chart");
    const points = series.filter(point => Number.isInteger(point.min_unit_cents) && point.min_unit_cents > 0);
    if (!points.length) {
      svg.innerHTML = '<text x="400" y="90" text-anchor="middle" class="chart-empty">Ainda não há preços legíveis para o gráfico.</text>';
      return;
    }
    const values = points.map(p => p.min_unit_cents);
    const low = Math.min(...values), high = Math.max(...values);
    const range = Math.max(high-low, Math.round(high * .05), 1);
    const xy = points.map((p,i) => ({
      x:90 + (points.length === 1 ? 310 : i/(points.length-1)*620),
      y:128 - (p.min_unit_cents-low)/range*90, point:p,
    }));
    const polyline = xy.map(p => p.x.toFixed(2)+","+p.y.toFixed(2)).join(" ");
    const circles = xy.map(p => '<circle cx="'+p.x+'" cy="'+p.y+'" r="4" class="chart-point"><title>'+escapeHtml(time(p.point.seen_at)+" · "+formatMoney(p.point.min_unit_cents)+(p.point.complete?"":" · consulta parcial"))+'</title></circle>').join("");
    svg.innerHTML = '<line x1="80" y1="145" x2="730" y2="145" class="chart-axis"/>'
      + '<text x="72" y="37" text-anchor="end" class="chart-label">'+escapeHtml(formatMoney(high))+'</text>'
      + '<text x="72" y="131" text-anchor="end" class="chart-label">'+escapeHtml(formatMoney(low))+'</text>'
      + '<polyline points="'+polyline+'" class="chart-line"/>'+circles
      + '<text x="90" y="170" class="chart-label">'+escapeHtml(time(points[0].seen_at))+'</text>'
      + '<text x="710" y="170" text-anchor="end" class="chart-label">'+escapeHtml(time(points.at(-1).seen_at))+'</text>';
  }

  function show(data) {
    const latest = data.latest;
    Q("#prices-empty").hidden = Boolean(latest);
    Q("#prices-content").hidden = !latest;
    if (!latest) return;
    Q("#prices-note").textContent = latest.note;
    Q("#prices-updated").textContent = "Última consulta: " + time(latest.seen_at);
    Q("#prices-coverage").innerHTML = Object.entries(latest.coverage).map(([key,info]) =>
      '<span class="coverage-badge '+(info.status === "ok" || info.status === "empty" ? "complete" : "partial")+'" title="'+escapeHtml(info.detail || "")+'">'
      + escapeHtml(storeName(key))+' · '+escapeHtml(coverageName[info.status] || info.status)
      +' · '+(info.pages || 0)+(key==='google_shopping'?' rodada(s)':' página(s)')+(info.priced != null?' · '+info.priced+' preço(s)':'')+(info.has_more?' · há mais resultados':'')+'</span>'
    ).join("");
    drawChart(data.series);
    const selected = latest.selected;
    Q("#price-offers").innerHTML = latest.offers.length ? latest.offers.map(offer => {
      const chosen = selected && selected.marketplace === offer.marketplace && selected.key === offer.key;
      return '<article class="offer-row'+(chosen?' chosen':'')+'"><div><span class="offer-store">'+escapeHtml(offer.merchant_name || storeName(offer.marketplace))+(chosen?(selected.selection_partial?' · Escolhida na comparação parcial':' · Escolhida automaticamente'):'')+'</span>'
        +'<strong>'+escapeHtml(offer.title)+'</strong></div><div class="offer-price"><strong>'+(offer.offer_kind === 'other_sellers' ? 'A partir de ' : '')+formatMoney(offer.unit_cents)+'</strong><small>por unidade'+(offer.coupon_available?' · cupom anunciado':'')+'</small></div>'
        +'<div class="offer-shipping"><strong>'+(offer.shipping_cents == null?'Frete a confirmar':formatMoney(offer.shipping_cents))+'</strong><small>'+escapeHtml(offer.shipping_note || "Conferir no checkout")+'</small></div>'
        +'<a class="button secondary" href="'+escapeHtml(offer.url)+'" target="_blank" rel="noopener noreferrer">Ver oferta '+icon("arrow")+'</a></article>';
    }).join("") : '<p class="tracking-note">Nenhuma oferta compatível foi encontrada nesta consulta.</p>';
  }

  window.refreshPriceHistory = async () => {
    if (!productId || !Q("#prices-dialog").open) return;
    const data = await api("/api/products/"+productId+"/prices");
    const signature = JSON.stringify(data);
    if (signature !== lastSignature) { lastSignature = signature; show(data); }
  };
  window.openPriceHistory = async product => {
    productId = product.id;
    lastSignature = "";
    Q("#prices-product").textContent = product.name + (product.mode === "search" ? " · "+product.query : " · "+product.asin);
    Q("#prices-empty").hidden = false;
    Q("#prices-content").hidden = true;
    Q("#prices-dialog").showModal();
    try { await window.refreshPriceHistory(); }
    catch (error) { toast(error.message,true); }
  };
  Q("#prices-dialog").addEventListener("close", () => { productId = null; lastSignature = ""; });
  window.updateTrackingMode();
})();
