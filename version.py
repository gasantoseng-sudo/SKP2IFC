"""Versão do app e de onde buscar atualizações.

Para publicar uma versão nova: aumente __version__, atualize o CHANGELOG.md e envie a tag
(ex.: v1.5.0) para o GitHub. O fluxo em .github/workflows/release.yml compila o .exe e cria a Release.
"""

__version__ = "1.4.0"
APP_NAME = "SKP2IFC"

# >>> Troque pelo seu repositório no GitHub (formato "usuario/repositorio"). <<<
GITHUB_REPO = "gasantoseng-sudo/skp2ifc"

ASSET_NAME = "SKP2IFC.exe"             # arquivo do .exe dentro da Release
HASH_ASSET_NAME = "SKP2IFC.exe.sha256"  # soma de verificação publicada junto
