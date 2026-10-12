"""V2 immutable index context. Stable world/dimension IDs are explicit deployment inputs.
Never derive identity from a changing level.dat hash or reuse another world's store.
"""
import re

KEYS={'world','dimension','renderer','caves','layoutSha256'}
HEX64=re.compile(r'[0-9a-f]{64}')

def validate_context(context):
    if not isinstance(context,dict) or set(context)!=KEYS:raise ValueError('invalid hires context fields')
    for key in ('world','dimension','renderer'):
        value=context[key]
        if (not isinstance(value,str) or not 1<=len(value)<=128 or value.strip()!=value
                or any(ord(c)<32 or ord(c)>126 for c in value)):
            raise ValueError(f'invalid hires context {key}')
    if type(context['caves']) is not bool:raise ValueError('invalid hires context caves')
    if not isinstance(context['layoutSha256'],str) or not HEX64.fullmatch(context['layoutSha256']):
        raise ValueError('invalid hires context layout hash')
    return dict(context)
