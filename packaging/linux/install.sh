#!/usr/bin/env bash
# PhoneBot — instalacja dla bieżącego użytkownika (bez sudo, bez dostępu do internetu).
#
#   ./install.sh               instaluje / aktualizuje program z tego folderu
#   ./install.sh --autostart   to samo + uruchamianie po zalogowaniu
#   ./install.sh --uninstall   usuwa program (dane, oferty i ustawienia zostają)
#
# Program trafia do ~/.local/opt/PhoneBot, polecenie `phonebot` do ~/.local/bin,
# a skrót do menu aplikacji i ikona do ~/.local/share. Dane: ~/.local/share/phonebot.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DEST="$HOME/.local/opt/PhoneBot"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"
AUTOSTART_DIR="$CONFIG_HOME/autostart"

say() { printf '%s\n' "$*"; }
fail() { printf 'BŁĄD: %s\n' "$*" >&2; exit 1; }

refresh_menu() {
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
}

ensure_not_running() {
    # po ścieżce pliku procesu (/proc/*/exe) — działa też, gdy uruchomiono go poleceniem `phonebot`
    local target exe
    [ -e "$DEST/PhoneBot" ] || return 0
    target="$(readlink -f "$DEST/PhoneBot")"
    for exe in /proc/[0-9]*/exe; do
        if [ "$(readlink "$exe" 2>/dev/null)" = "$target" ]; then
            fail "PhoneBot jest uruchomiony. Zamknij go (menu ikony → Zakończ) i uruchom skrypt ponownie."
        fi
    done
}

uninstall() {
    ensure_not_running
    rm -rf "$DEST" "$DEST.new" "$DEST.old"
    if [ -L "$BIN_DIR/phonebot" ]; then rm -f "$BIN_DIR/phonebot"; fi
    rm -f "$APPS_DIR/phonebot.desktop" "$ICON_DIR/phonebot.png" "$AUTOSTART_DIR/phonebot.desktop"
    refresh_menu
    say "PhoneBot usunięty."
    say "Dane (oferty, ustawienia, modele AI) zostały w: $DATA_HOME/phonebot"
    say "Usuń ten folder ręcznie, jeśli nie są już potrzebne. Token Telegram jest w pęku kluczy systemu"
    say "(„Hasła i klucze” / KWallet, wpisy „PhoneBot: …”) albo w pliku secrets.json w folderze danych."
}

check_libraries() {
    # Qt potrzebuje kilku bibliotek systemowych X11/xcb, których nie ma w paczce — sprawdź i podpowiedz.
    local libs missing
    libs=("$DEST/_internal/PySide6/Qt/plugins/platforms/libqxcb.so")
    libs+=("$DEST"/_internal/PySide6/Qt/lib/libQt6XcbQpa.so*)
    command -v ldd >/dev/null 2>&1 || return 0
    # LD_LIBRARY_PATH jak przy starcie programu: biblioteki dołączone do paczki leżą w _internal
    missing="$(for f in "${libs[@]}"; do
                   if [ -e "$f" ]; then LD_LIBRARY_PATH="$DEST/_internal" ldd "$f" 2>/dev/null || true; fi
               done | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')"
    if [ -n "$missing" ]; then
        say ""
        say "UWAGA: brakuje bibliotek systemowych potrzebnych do wyświetlenia okna: $missing"
        say "Zainstaluj je jednym poleceniem (wymaga hasła administratora):"
        say "  Ubuntu / Debian / Mint:  sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 \\"
        say "                           libxcb-keysyms1 libxcb-image0 libxcb-render-util0 libegl1"
        say "  Fedora:                  sudo dnf install xcb-util-cursor xcb-util-wm xcb-util-keysyms \\"
        say "                           xcb-util-image xcb-util-renderutil libxkbcommon-x11 mesa-libEGL"
        say "  Arch / Manjaro:          sudo pacman -S xcb-util-cursor xcb-util-wm xcb-util-keysyms \\"
        say "                           xcb-util-image xcb-util-renderutil libxkbcommon-x11"
    fi
}

install_app() {
    local autostart="$1"
    [ -x "$SRC/PhoneBot" ] && [ -d "$SRC/_internal" ] \
        || fail "uruchom skrypt z rozpakowanego folderu PhoneBot (obok pliku PhoneBot i folderu _internal)."
    if [ "$SRC" = "$DEST" ]; then
        say "Program jest już zainstalowany w $DEST — odświeżam tylko skrót i ikonę."
    else
        ensure_not_running
        mkdir -p "$(dirname "$DEST")"
        rm -rf "$DEST.new" "$DEST.old"
        cp -a "$SRC" "$DEST.new"  # najpierw kopia obok — przerwana instalacja nie psuje działającej wersji
        if [ -d "$DEST" ]; then mv "$DEST" "$DEST.old"; fi
        mv "$DEST.new" "$DEST"
        rm -rf "$DEST.old"
    fi

    mkdir -p "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
    ln -sfn "$DEST/PhoneBot" "$BIN_DIR/phonebot"
    if [ -f "$DEST/phonebot.png" ]; then cp -f "$DEST/phonebot.png" "$ICON_DIR/phonebot.png"; fi
    # ścieżka w cudzysłowie: działa też, gdy katalog domowy zawiera spacje
    sed "s|@EXEC@|\"$DEST/PhoneBot\"|" "$DEST/phonebot.desktop" > "$APPS_DIR/phonebot.desktop"
    chmod 644 "$APPS_DIR/phonebot.desktop"
    if [ "$autostart" = 1 ]; then
        mkdir -p "$AUTOSTART_DIR"
        cp -f "$APPS_DIR/phonebot.desktop" "$AUTOSTART_DIR/phonebot.desktop"
    fi
    refresh_menu

    say "PhoneBot zainstalowany w: $DEST"
    say "Uruchom z menu aplikacji („PhoneBot”) albo poleceniem: phonebot"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) say "(Folder $BIN_DIR nie jest w PATH — po ponownym zalogowaniu zwykle już jest;"
           say " do tego czasu uruchamiaj: $DEST/PhoneBot)" ;;
    esac
    if [ "$autostart" = 1 ]; then say "Autostart: włączony (program uruchomi się po zalogowaniu)."; fi
    check_libraries
}

case "${1:-}" in
    "") install_app 0 ;;
    --autostart) install_app 1 ;;
    --uninstall) uninstall ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) fail "nieznana opcja: $1 (dostępne: --autostart, --uninstall, --help)" ;;
esac
