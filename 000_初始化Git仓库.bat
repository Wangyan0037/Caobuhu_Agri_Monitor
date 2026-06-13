@echo off
chcp 65001 >nul
title 草埠湖农业监测系统 - Git 仓库初始化
echo ============================================
echo  草埠湖农业监测系统 - Git 版本管理
echo ============================================
echo.

:: 清理可能残留的部分初始化的 .git 目录
if exist ".git" (
    echo [INFO] 发现残留的 .git 目录，正在清理...
    rmdir /s /q ".git" 2>nul
    if exist ".git" (
        attrib -R .git\* /S 2>nul
        rmdir /s /q ".git" 2>nul
    )
)

echo [1/4] 初始化 Git 仓库...
git init
if %errorlevel% neq 0 (
    echo.
    echo [错误] Git 初始化失败！可能原因:
    echo   1. 未安装 Git - 请从 https://git-scm.com/downloads/win 下载安装
    echo   2. .git 目录被锁定 - 尝试重启电脑后重试
    echo.
    pause
    exit /b 1
)

echo [2/4] 添加所有文件到暂存区...
git add -A
if %errorlevel% neq 0 ( echo [错误] 添加文件失败！ & pause & exit /b 1 )

echo [3/4] 创建初始提交...
git commit -m "草埠湖农业监测系统 v1.0 (升级前快照)"
if %errorlevel% neq 0 ( echo [错误] 提交失败！ & pause & exit /b 1 )

echo [4/4] 创建版本标签...
git tag -a before-upgrade -m "升级前快照 (初始化版本)"

echo.
echo ============================================
echo  ✓ Git 仓库初始化完成！
echo ============================================
echo.
git log --oneline --decorate -3
echo.
echo 当前干净状态: & git status --short
echo.
echo ============================================
echo  升级前后操作指南
echo ============================================
echo.
echo  升级前确认: git status
echo  标记版本:   git tag -a before-upgrade -m "升级前快照"
echo  升级后提交: git add -A ^&^& git commit -m "系统升级 vX.X"
echo.
echo  升级失败回滚: ^>^>^>  git checkout before-upgrade  ^<^<^<
echo.
echo  查看所有标签: git tag -l
echo  查看差异:   git diff before-upgrade
echo.
pause
