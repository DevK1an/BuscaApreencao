"""Substituição atômica com recuperação curta de bloqueios transitórios do Windows."""
import os
import time


def replace_file(source, target, attempts=5):
    for attempt in range(attempts):
        try:
            os.replace(source,target)
            return
        except PermissionError as error:
            if getattr(error,"winerror",None) not in (5,32,33) or attempt == attempts-1:
                raise
            # Não altera permissões nem remove o arquivo anterior.
            time.sleep(min(0.02 * 2**attempt,0.2))
