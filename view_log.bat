@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$host.UI.RawUI.WindowTitle = 'Playwright Live Log Monitor'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Host '==================================================='; Write-Host '  Playwright Live Log Monitor (UTF-8)'; Write-Host '==================================================='; Write-Host ''; Get-Content '%~dp0playwright_debug.log' -Encoding UTF8 -Wait -Tail 30"
