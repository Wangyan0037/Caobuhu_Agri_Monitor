@echo off
chcp 65001 >nul
title 草埠湖 - 清理 Git 残留目录

echo 正在清理可能残留的部分初始化 .git 目录...
echo.

:: 清理主 .git 目录
if exist ".git" (
    echo [1/2] 清理 .git 目录...
    attrib -R .git\* /S 2>nul
    rmdir /s /q ".git" 2>nul
    if exist ".git" ( echo   ! 删除失败，请手动删除 ) else ( echo   ✓ 已删除 )
) else ( echo [1/2] .git 目录不存在，跳过 )

:: 清理 .git_repo 目录
if exist ".git_repo" (
    echo [2/2] 清理 .git_repo 目录...
    attrib -R .git_repo\* /S 2>nul
    rmdir /s /q ".git_repo" 2>nul
    if exist ".git_repo" ( echo   ! 删除失败，请手动删除 ) else ( echo   ✓ 已删除 )
) else ( echo [2/2] .git_repo 目录不存在，跳过 )

echo.
echo 清理完成！现在可以运行 000_初始化Git仓库.bat
pause