"""Avisos locais exibidos no navegador; não enviam dados para outros serviços."""


def show_status(page, title, detail="", tone="info"):
    from playwright.sync_api import Error
    from runtime import check_stop, emit_status

    check_stop()
    emit_status(title, detail, tone)
    if page.is_closed():
        return
    try:
        page.evaluate(
            """({title, detail, tone}) => {
                let host = document.getElementById('amazon-bot-status');
                if (!host) {
                    host = document.createElement('div');
                    host.id = 'amazon-bot-status';
                    host.style.cssText = 'position:fixed;bottom:16px;left:16px;'
                        + 'z-index:2147483647;pointer-events:none;max-width:370px;';
                    const root = host.attachShadow({mode:'open'});
                    root.innerHTML = '<style>:host{font:14px/1.5 Arial,sans-serif}'
                        + 'section{background:#fff;color:#17202a;padding:14px 16px;'
                        + 'border:2px solid #2471a3;border-radius:10px;'
                        + 'box-shadow:0 3px 16px #0003}strong{display:block}'
                        + 'p{margin:6px 0 0;white-space:pre-wrap}</style>'
                        + '<section role="status" aria-live="polite">'
                        + '<strong></strong><p></p></section>';
                    document.documentElement.appendChild(host);
                }
                const root = host.shadowRoot;
                root.querySelector('strong').textContent = title;
                root.querySelector('p').textContent = detail;
                root.querySelector('section').style.borderColor =
                    {info:'#2471a3', warning:'#b9770e', success:'#1e8449'}[tone]
                    || '#2471a3';
            }""",
            {"title": title, "detail": detail, "tone": tone},
        )
    except Error:
        # O aviso não pode interromper navegações, fechamento ou validação do pedido.
        pass
