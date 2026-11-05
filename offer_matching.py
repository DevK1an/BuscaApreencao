"""Compatibilidade de títulos, modelo e categoria do produto procurado."""
import re
import unicodedata

STOP = {"a","o","as","os","de","da","do","das","dos","e","com","para","um","uma","por","original","novo","nova"}
VARIANTS = {"lite","oled","pro","plus","max","mini","ultra","digital"}
ACCESSORIES = {"capa","case","pelicula","suporte","carregador","cabo","adesivo","bolsa","estojo","controle","kit","combo","replica"}
USED = {"usado","usada","seminovo","seminova","recondicionado","recondicionada","renovado","renovada"}
HARDWARE_WORDS = {"console","consola","consoles","videogame"}
GAME_WORDS = {"jogo","jogos","game","games","software"}


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFD",text.lower()) if unicodedata.category(c)!="Mn")


def tokens(text):
    return set(re.findall(r"[a-z]+|[0-9]+",normalized(text)))


CONSOLE_DESCRIPTORS = {
    "nintendo","switch","playstation","ps","xbox","microsoft","sony","series","x","s",
    "1","2","3","4","5","v2","lite","oled","digital","pro","slim","gb","tb",
    "32","64","128","256","512","1024","nacional","internacional","importado",
    "portatil","preto","branco",
}


def console_query(query):
    words=tokens(query)
    platform=bool(words & {"switch","playstation","ps","xbox"})
    if not platform or words & ACCESSORIES:
        return False
    return bool(words & HARDWARE_WORDS) or (
        not words & GAME_WORDS and (
            "bundle" in words or (words-STOP).issubset(CONSOLE_DESCRIPTORS)
        )
    )


def match_reason(query,title):
    query_words=tokens(query)
    actual=tokens(title)
    hardware=console_query(query)
    wanted=query_words-STOP
    if hardware:
        wanted-=HARDWARE_WORDS
    if len(wanted)<2 or not wanted.issubset(actual):
        return "missing_terms"
    q=normalized(query)
    t=normalized(title)
    # O 2 de um título de jogo ou de V2 não identifica um Switch 2.
    switch=re.search(r"\bswitch\W*([0-9]+)\b",q)
    if switch and not re.search(rf"\bswitch\W*{re.escape(switch[1])}\b",t):
        return "different_model"
    playstation=re.search(r"\b(?:playstation|ps)\W*([0-9]+)\b",q)
    if playstation and not re.search(rf"\b(?:playstation|ps)\W*{re.escape(playstation[1])}\b",t):
        return "different_model"
    xbox=re.search(r"\bxbox\s+series\s+([xs])\b",q)
    if xbox and not re.search(rf"\bxbox\s+series\s+{xbox[1]}\b",t):
        return "different_model"
    if actual & USED and not query_words & USED:
        return "used"
    if ((actual & VARIANTS)|(actual & ACCESSORIES))-query_words:
        return "variant_or_accessory"
    if hardware:
        prefix=bool(re.match(r"^\s*(?:(?:gaming\s+)?console|consola|videogame|video\s+game)\b",t))
        bundle=(
            "bundle" in actual
            and bool(re.match(r"^\s*(?:bundle\s+)?(?:nintendo\s+)?switch\W*[0-9]+\b",t))
        )
        bare=actual.issubset(query_words|STOP|HARDWARE_WORDS|{"gb","256","512","1024","nacional","internacional","importado","portatil","preto","branco"})
        if actual & GAME_WORDS and not prefix and not bundle:
            return "not_console"
        if not prefix and not bundle and not actual & HARDWARE_WORDS and not bare:
            return "not_console"
    return None


def matches(query,title):
    return match_reason(query,title) is None
