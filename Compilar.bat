@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Compilar SKP2IFC
where python >nul 2>nul || (
  echo Python nao encontrado. Instale em https://www.python.org/downloads/
  echo marcando "Add python.exe to PATH" e deixando o tcl/tk marcado. Depois de instalar, de dois cliques aqui de novo.
  pause & exit /b 1
)
echo Instalando dependencias (pode demorar alguns minutos)...
python -m pip install -r requirements.txt || goto :err
echo Gerando o executavel...
python -m PyInstaller --noconfirm --onefile --windowed --name SKP2IFC --collect-all ifcopenshell --collect-all openskp --collect-all trimesh --collect-all fast_simplification --hidden-import PIL.ImageTk app.py || goto :err
echo.
echo PRONTO: o programa esta em dist\SKP2IFC.exe
explorer dist
pause & exit /b 0
:err
echo.
echo Algo falhou. Tire um print desta janela e me envie.
pause & exit /b 1
