"""URLs de lojas públicas e exclusões por nome/domínio."""
import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit, urlunsplit, unquote

DEFAULT_EXCLUDED = ("AliExpress", "Shopee")
GOOGLE_HOSTS = {"google.com", "www.google.com", "google.com.br", "www.google.com.br"}
ALIASES = {
    "aliexpress": ("aliexpress.com", "aliexpress.com.br"),
    "shopee": ("shopee.com.br", "shopee.com"),
    "amazon": ("amazon.com.br",),
    "mercadolivre": ("mercadolivre.com.br",),
    "magalu": ("magazineluiza.com.br", "magalu.com"),
    "magazineluiza": ("magazineluiza.com.br", "magalu.com"),
    "kabum": ("kabum.com.br",),
    "carrefour": ("carrefour.com.br",),
    "casasbahia": ("casasbahia.com.br",),
}


def words(value):
    plain="".join(c for c in unicodedata.normalize("NFD",value.casefold()) if unicodedata.category(c)!="Mn")
    return " ".join(re.findall(r"[a-z0-9]+",plain))


def host_matches(host, domain):
    return host == domain or host.endswith("."+domain)


def merchant_url(value):
    if not isinstance(value,str) or not value or len(value)>12000 or any(ord(c)<32 for c in value):
        raise ValueError("Link de loja inválido.")
    p=urlsplit(value.strip())
    try:
        host=(p.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if p.scheme!="https" or p.username or p.password or p.port not in (None,443):
            raise ValueError()
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",host):
            raise ValueError()
        if any(host_matches(host,d) for d in ("localhost","local","internal","test","invalid")):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
    except (ValueError,UnicodeError):
        raise ValueError("Use um link HTTPS de uma loja pública, sem usuário ou senha.") from None
    return urlunsplit(("https",host,p.path or "/",p.query,""))


def normalize_exclusions(value):
    if not isinstance(value,list) or len(value)>100:
        raise ValueError("Informe até 100 lojas excluídas.")
    result=[]
    seen=set()
    for entry in value:
        if not isinstance(entry,str) or not entry.strip() or len(entry)>160 or any(ord(c)<32 for c in entry):
            raise ValueError("Informe nomes ou domínios válidos nas lojas excluídas.")
        label=entry.strip()
        if "://" in label or "/" in label:
            label=urlsplit(merchant_url(label if "://" in label else "https://"+label)).hostname.removeprefix("www.")
        elif "." in label and " " not in label:
            label=urlsplit(merchant_url("https://"+label)).hostname.removeprefix("www.")
        elif not re.fullmatch(r"[\w\s&!.'-]+",label,flags=re.UNICODE):
            raise ValueError("Use o nome ou domínio da loja, sem padrões ou códigos.")
        key=words(label)
        if len(key.replace(" ",""))<2:
            raise ValueError("O nome da loja excluída deve ter pelo menos dois caracteres.")
        if key not in seen:
            seen.add(key)
            result.append(label)
    return tuple(result)


def excluded_store(name,url,excluded):
    host=(urlsplit(url).hostname or "").lower().rstrip(".")
    normalized=words(name)
    compact=normalized.replace(" ","")
    for item in excluded:
        needle=words(item)
        key=needle.replace(" ","")
        domain=item.lower().removeprefix("www.")
        if "." in domain and " " not in domain and host_matches(host,domain):
            return True
        if needle==normalized or (" "+needle+" ") in (" "+normalized+" "):
            return True
        if key in ALIASES:
            if any(host_matches(host,d) for d in ALIASES[key]):
                return True
            if compact.startswith(key) or (key=="amazon" and compact.startswith("amazoncom")):
                return True
        # Um domínio digitado também pode corresponder ao nome público da loja.
        for alias,domains in ALIASES.items():
            if domain in domains and (compact.startswith(alias) or any(host_matches(host,d) for d in domains)):
                return True
    return False


def google_url(value):
    p=urlsplit(value)
    return p.scheme=="https" and p.hostname in GOOGLE_HOSTS


def unwrap_google_link(value,base="https://www.google.com"):
    from urllib.parse import urljoin
    url=urljoin(base,value)
    for _ in range(4):
        if not google_url(url):
            return merchant_url(url)
        p=urlsplit(url)
        values=parse_qs(p.query)
        next_url=next((values[k][0] for k in ("adurl","url","q") if values.get(k) and values[k][0].startswith("https://")),None)
        if not next_url:
            if p.path not in ("/goto","/url","/aclk"):
                raise ValueError("O link ainda é uma página de produto do Google, não uma loja.")
            return url
        url=unquote(next_url)
    raise ValueError("Redirecionamento de loja excessivo.")


def offer_key(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
