# SKP → IFC

Converte arquivos `.skp` em `.ifc` **sem precisar do SketchUp instalado**
(leitor open source OpenSKP + escrita com ifcopenshell).

## Rodar
    pip install -r requirements.txt
    python app.py                       # janela
    python app.py --cli arq.skp -u mm   # linha de comando (veja --help em cli.py)

## Gerar o .exe (Windows)
    build_exe.bat      ->  dist\SKP2IFC.exe

## O que o IFC contém
- Tudo como `IfcBuildingElementProxy` (referência de arquitetura), um por grupo/componente.
- Geometria triangulada com cor/transparência dos materiais do SketchUp.
- Pset `SKP_Info` em cada elemento: Tag, Caminho (hierarquia de grupos), Definição.
- Unidade (m/cm/mm), esquema (IFC2X3 padrão ou IFC4), deslocamento de origem e
  filtro por tag (palavras-chave + lista marcável). Tags ocultas no SKP são ignoradas por padrão.
- Linhas/guias soltas (sem faces) não são exportadas.

## Limitações conhecidas
- Hierarquia de grupos fica "achatada" (os caminhos vão no Pset, não em IfcRelAggregates).
- Dois grupos irmãos com o mesmo nome e mesma tag viram um só elemento.
- Texturas (imagens) não são exportadas, só a cor média do material.

## Otimização do tamanho do IFC
- Vértices duplicados são unidos (sem perda) e cada elemento guarda uma lista de pontos só.
- "Máx. de triângulos por elemento" (padrão 30000; 0 = sem limite): elementos acima disso têm a
  malha simplificada. O log lista cada elemento simplificado (antes -> depois).
  Num caso real (508 mil triângulos -> 30 mil) o desvio medido foi ~1 mm de mediana e 8 mm no p99.

## Aba "Otimizar IFC" (para IFCs que já existem)

Funciona com qualquer `.ifc` (IFC2X3 ou IFC4), de qualquer programa. O original nunca é alterado:
o resultado sai como `nome_otimizado.ifc`.

- **Sempre (sem perda):** une vértices e pontos duplicados.
- **Se passar do limite de triângulos** (padrão 30000 por representação): simplifica a malha.
  Use `0` para só fazer a limpeza sem perda.
- **Mantém:** classes, nomes, GlobalIds, propriedades (Psets), tipos, materiais, estilos e hierarquia.
- **Não mexe:** extrusões/paredes paramétricas. Sólidos facetados (Brep) só têm pontos unidos.
- Malhas simplificadas perdem as normais gravadas (o Eberick recalcula).

Linha de comando: `python app.py --cli optimize modelo.ifc -o saida --max-tri 30000 --keep 30`

"Manter só X%" reduz cada malha proporcionalmente (malhas com menos de 300 triângulos ficam intactas). Malhas abertas ou com muitas peças soltas (folhagem, gente) simplificam pouco; o jeito mais eficaz de aliviar o arquivo é excluir essas tags na conversão do SKP.

## Aba "Visualizar origem"

Abre um `.skp` ou `.ifc` e mostra **planta** e **vista 3D** (amostra de pontos, leve mesmo em modelos de milhões
de triângulos), com os eixos X (vermelho), Y (verde) e Z (azul) na origem (0,0,0), a caixa envolvente e as
coordenadas dos cantos mínimo/máximo. Cores por tag (SKP) ou por tag/classe (IFC); clique na lista para ocultar.

- 3D: arraste para girar, roda para zoom, botão direito para mover. Planta: arraste para mover, roda para zoom.
- Deslocamento de origem: digite X/Y/Z (na unidade escolhida) ou marque "Canto mínimo na origem" e veja onde o
  modelo cai em relação à origem; "Enviar para aba SKP → IFC" copia os valores para a conversão.
- Em IFC grandes a leitura pode levar alguns minutos (a barra mostra o andamento).

## Versões e atualização automática

O app mostra a versão no título e tem o botão **Verificar atualizações** (e a opção "Verificar ao abrir").
Ele consulta a última Release do GitHub, baixa o `SKP2IFC.exe`, confere o SHA-256 publicado junto e troca o
programa sozinho (o antigo fica como `.old` por alguns instantes e é apagado na próxima abertura).
