"""Bounded, read-only validation of selected block PNG entries in an open JAR."""
import hashlib
import zipfile
import zlib
import atlas_identity
import texture_png

MAX_TEXTURE_BYTES = 4 * 1024 * 1024
MAX_TEXTURE_PIXELS = 4 * 1024 * 1024
MAX_TOTAL_TEXTURE_BYTES = 512 * 1024 * 1024


def verify(archive, info, names, budget):
    """No extraction. Budget is shared across all archives in one inventory."""
    result = dict(state='invalid', payloadVerified=False, staticSquareCandidate=False)
    if info.flag_bits & 1:
        return dict(result, reason='Encrypted texture')
    if not 1 <= info.file_size <= MAX_TEXTURE_BYTES:
        return dict(result, state='limit', reason='Texture exceeds encoded byte limit or is empty')
    if budget['bytes'] + info.file_size > MAX_TOTAL_TEXTURE_BYTES:
        return dict(result, state='limit', reason='Inventory texture byte budget exhausted')
    # Reserve declared bytes even if decoding fails; failures cannot evade the run budget.
    budget['bytes'] += info.file_size
    try:
        with archive.open(info) as stream:
            raw = stream.read(info.file_size + 1)
        if len(raw) != info.file_size:
            raise ValueError('Texture length differs from ZIP entry')
        width, height = atlas_identity._png_size(raw, 'texture')
        result.update(width=width, height=height, sha256=hashlib.sha256(raw).hexdigest(),
                      animationMetadataPresent=info.filename + '.mcmeta' in names)
        if width * height > MAX_TEXTURE_PIXELS:
            return dict(result, state='limit', reason='Texture exceeds pixel limit')
        result.update(texture_png.verify(raw, MAX_TEXTURE_PIXELS))
        return dict(result, state='verified', payloadVerified=True,
                    staticSquareCandidate=width == height and not result['animationMetadataPresent'])
    except texture_png.UnsupportedPng as error:
        return dict(result, state='unsupported', reason=str(error))
    except (ValueError, OSError, EOFError, RuntimeError, zipfile.BadZipFile, NotImplementedError, zlib.error) as error:
        return dict(result, reason=str(error)[:300])


def summary(textures):
    checks = [t['validation'] for t in textures if 'validation' in t]
    return dict(checked=len(checks), verified=sum(c['state'] == 'verified' for c in checks),
                invalid=sum(c['state'] == 'invalid' for c in checks),
                unsupported=sum(c['state'] == 'unsupported' for c in checks),
                limited=sum(c['state'] == 'limit' for c in checks),
                staticSquareCandidates=sum(c['staticSquareCandidate'] for c in checks),
                animationMetadataPresent=sum(c.get('animationMetadataPresent', False) for c in checks),
                issues=[dict(resource=t['resource'], entry=t['entry'], **t['validation'])
                        for t in textures if 'validation' in t and t['validation']['state'] != 'verified'])
