#!/bin/bash

# ============================================
# 🌐 Lavalink IPv6 Rotation Setup Script
# ============================================
# Automatiza a configuração de rotação IPv6
# para Lavalink com youtube-plugin
# ============================================

set -e

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configurações padrão
DEFAULT_INTERFACE="enp3s0"
CONFIG_FILE="/etc/ndppd.conf"
SYSCTL_FILE="/etc/sysctl.d/99-ipv6-rotation.conf"
SYSTEMD_SERVICE="/etc/systemd/system/ipv6-route.service"
LAVALINK_DIR="$HOME/lavav6"

# ============================================
# Funções de utilidade
# ============================================

print_header() {
    echo ""
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}============================================${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "Este script precisa ser executado como root (sudo)"
        exit 1
    fi
}

get_current_ipv6_block() {
    if [[ -f "$CONFIG_FILE" ]]; then
        grep -oP 'rule \K[0-9a-fA-F:]+::/64' "$CONFIG_FILE" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

get_interface() {
    if [[ -f "$CONFIG_FILE" ]]; then
        grep -oP 'proxy \K[a-zA-Z0-9]+' "$CONFIG_FILE" 2>/dev/null || echo "$DEFAULT_INTERFACE"
    else
        echo "$DEFAULT_INTERFACE"
    fi
}

validate_ipv6_block() {
    local block="$1"
    # Validação básica de formato IPv6 com /64
    if [[ $block =~ ^[0-9a-fA-F:]+::/64$ ]]; then
        return 0
    else
        return 1
    fi
}

extract_prefix() {
    local block="$1"
    echo "${block%::/64}"
}

# ============================================
# Funções de instalação
# ============================================

install_ndppd() {
    print_info "Instalando ndppd..."
    apt-get update -qq
    apt-get install -y ndppd
    print_success "ndppd instalado"
}

configure_ndppd() {
    local prefix="$1"
    local interface="$2"
    
    print_info "Configurando ndppd..."
    
    cat > "$CONFIG_FILE" << EOF
proxy $interface {
    rule ${prefix}::/64 {
        static
    }
}
EOF
    
    print_success "Configuração salva em $CONFIG_FILE"
}

configure_sysctl() {
    print_info "Configurando sysctl..."
    
    cat > "$SYSCTL_FILE" << EOF
# IPv6 Rotation for Lavalink
net.ipv6.ip_nonlocal_bind=1
EOF
    
    sysctl -p "$SYSCTL_FILE" > /dev/null 2>&1
    print_success "sysctl configurado"
}

configure_systemd_service() {
    local prefix="$1"
    
    print_info "Criando serviço systemd para rota local..."
    
    cat > "$SYSTEMD_SERVICE" << EOF
[Unit]
Description=Add local IPv6 route for Lavalink rotation
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/ip -6 route add local ${prefix}::/64 dev lo
ExecStop=/sbin/ip -6 route del local ${prefix}::/64 dev lo
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    
    systemctl daemon-reload
    systemctl enable ipv6-route.service
    print_success "Serviço systemd criado e habilitado"
}

add_local_route() {
    local prefix="$1"
    
    print_info "Adicionando rota local..."
    
    # Remove rota antiga se existir
    ip -6 route del local ${prefix}::/64 dev lo 2>/dev/null || true
    
    # Adiciona nova rota
    ip -6 route add local ${prefix}::/64 dev lo 2>/dev/null || {
        print_warning "Rota já existe ou erro ao adicionar"
    }
    
    print_success "Rota local configurada"
}

start_services() {
    print_info "Iniciando serviços..."
    
    systemctl restart ndppd
    systemctl start ipv6-route.service 2>/dev/null || true
    
    print_success "Serviços iniciados"
}

test_connectivity() {
    local prefix="$1"
    
    print_info "Testando conectividade IPv6..."
    echo ""
    
    # Testa com IP aleatório
    local test_ip="${prefix}::cafe"
    echo -e "Testando com IP: ${CYAN}$test_ip${NC}"
    
    if ping6 -I "$test_ip" google.com -c 3 -W 5 > /dev/null 2>&1; then
        print_success "Conectividade IPv6 funcionando!"
        return 0
    else
        print_error "Falha na conectividade IPv6"
        print_warning "Verifique se o ndppd está rodando corretamente"
        return 1
    fi
}

# ============================================
# Funções do menu
# ============================================

full_install() {
    print_header "🚀 Instalação Completa"
    
    check_root
    
    # Detectar interface
    echo -e "Interfaces de rede disponíveis:"
    ip link show | grep -E "^[0-9]+" | awk '{print "  " $2}' | tr -d ':'
    echo ""
    
    read -p "Digite a interface de rede [$DEFAULT_INTERFACE]: " interface
    interface="${interface:-$DEFAULT_INTERFACE}"
    
    # Solicitar bloco IPv6
    echo ""
    echo -e "${YELLOW}Exemplo de bloco: 2804:14d:7e3a:82da::/64${NC}"
    read -p "Digite seu bloco IPv6 (com /64): " ipv6_block
    
    if ! validate_ipv6_block "$ipv6_block"; then
        print_error "Formato de bloco IPv6 inválido!"
        print_info "Use o formato: xxxx:xxxx:xxxx:xxxx::/64"
        exit 1
    fi
    
    local prefix=$(extract_prefix "$ipv6_block")
    
    echo ""
    print_info "Configuração:"
    echo "  Interface: $interface"
    echo "  Bloco IPv6: $ipv6_block"
    echo "  Prefixo: $prefix"
    echo ""
    
    read -p "Confirmar instalação? [y/N]: " confirm
    if [[ ! $confirm =~ ^[Yy]$ ]]; then
        print_warning "Instalação cancelada"
        exit 0
    fi
    
    echo ""
    
    # Executar instalação
    install_ndppd
    configure_ndppd "$prefix" "$interface"
    configure_sysctl
    configure_systemd_service "$prefix"
    add_local_route "$prefix"
    start_services
    
    echo ""
    test_connectivity "$prefix"
    
    echo ""
    print_header "✅ Instalação Concluída"
    echo ""
    echo -e "Agora configure o ${CYAN}application.yml${NC} do Lavalink:"
    echo ""
    echo -e "${YELLOW}lavalink:"
    echo "  server:"
    echo "    ratelimit:"
    echo "      ipBlocks:"
    echo "        - \"${ipv6_block}\""
    echo "      strategy: \"RotatingNanoSwitch\""
    echo "      searchTriggersFail: true"
    echo -e "      retryLimit: -1${NC}"
    echo ""
    echo -e "Inicie o Lavalink com:"
    echo -e "${CYAN}java -Djava.net.preferIPv6Addresses=true -Djava.net.preferIPv4Stack=false -jar Lavalink.jar${NC}"
}

update_block() {
    print_header "🔄 Atualizar Bloco IPv6"
    
    check_root
    
    local current_block=$(get_current_ipv6_block)
    local current_interface=$(get_interface)
    
    if [[ -n "$current_block" ]]; then
        echo -e "Bloco atual: ${CYAN}$current_block${NC}"
        echo -e "Interface: ${CYAN}$current_interface${NC}"
    else
        print_warning "Nenhuma configuração encontrada. Execute a instalação completa primeiro."
        exit 1
    fi
    
    echo ""
    read -p "Digite o novo bloco IPv6 (com /64): " new_block
    
    if ! validate_ipv6_block "$new_block"; then
        print_error "Formato de bloco IPv6 inválido!"
        exit 1
    fi
    
    local new_prefix=$(extract_prefix "$new_block")
    local old_prefix=$(extract_prefix "$current_block")
    
    echo ""
    print_info "Atualizando de $current_block para $new_block"
    
    read -p "Confirmar atualização? [y/N]: " confirm
    if [[ ! $confirm =~ ^[Yy]$ ]]; then
        print_warning "Atualização cancelada"
        exit 0
    fi
    
    # Remover rota antiga
    ip -6 route del local ${old_prefix}::/64 dev lo 2>/dev/null || true
    
    # Atualizar configurações
    configure_ndppd "$new_prefix" "$current_interface"
    configure_systemd_service "$new_prefix"
    add_local_route "$new_prefix"
    start_services
    
    echo ""
    test_connectivity "$new_prefix"
    
    echo ""
    print_header "✅ Bloco Atualizado"
    echo ""
    echo -e "${YELLOW}IMPORTANTE: Atualize também o application.yml do Lavalink!${NC}"
    echo ""
    echo "Arquivos atualizados automaticamente:"
    echo "  ✓ /etc/ndppd.conf"
    echo "  ✓ /etc/systemd/system/ipv6-route.service"
    echo ""
    echo "Arquivo que você precisa atualizar manualmente:"
    echo "  → application.yml (ipBlocks)"
}

check_status() {
    print_header "📊 Status da Configuração"
    
    local current_block=$(get_current_ipv6_block)
    local current_interface=$(get_interface)
    
    echo -e "${BLUE}Configuração:${NC}"
    if [[ -n "$current_block" ]]; then
        echo -e "  Bloco IPv6: ${GREEN}$current_block${NC}"
        echo -e "  Interface: ${GREEN}$current_interface${NC}"
    else
        echo -e "  ${RED}Não configurado${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}Serviços:${NC}"
    
    # ndppd
    if systemctl is-active --quiet ndppd; then
        echo -e "  ndppd: ${GREEN}● Ativo${NC}"
    else
        echo -e "  ndppd: ${RED}○ Inativo${NC}"
    fi
    
    # ipv6-route
    if systemctl is-active --quiet ipv6-route; then
        echo -e "  ipv6-route: ${GREEN}● Ativo${NC}"
    else
        echo -e "  ipv6-route: ${RED}○ Inativo${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}Sistema:${NC}"
    
    # ip_nonlocal_bind
    local nonlocal_bind=$(sysctl -n net.ipv6.ip_nonlocal_bind 2>/dev/null || echo "0")
    if [[ "$nonlocal_bind" == "1" ]]; then
        echo -e "  ip_nonlocal_bind: ${GREEN}Habilitado${NC}"
    else
        echo -e "  ip_nonlocal_bind: ${RED}Desabilitado${NC}"
    fi
    
    # Rota local
    if [[ -n "$current_block" ]]; then
        local prefix=$(extract_prefix "$current_block")
        if ip -6 route show | grep -q "local ${prefix}::/64"; then
            echo -e "  Rota local: ${GREEN}Configurada${NC}"
        else
            echo -e "  Rota local: ${RED}Não encontrada${NC}"
        fi
    fi
    
    echo ""
    
    # Teste de conectividade
    if [[ -n "$current_block" ]]; then
        local prefix=$(extract_prefix "$current_block")
        echo -e "${BLUE}Teste de Conectividade:${NC}"
        if ping6 -I "${prefix}::cafe" google.com -c 1 -W 3 > /dev/null 2>&1; then
            echo -e "  ${GREEN}✓ IPv6 funcionando${NC}"
        else
            echo -e "  ${RED}✗ IPv6 não está funcionando${NC}"
        fi
    fi
}

restart_services() {
    print_header "🔃 Reiniciando Serviços"
    
    check_root
    
    print_info "Reiniciando ndppd..."
    systemctl restart ndppd
    
    print_info "Reiniciando ipv6-route..."
    systemctl restart ipv6-route 2>/dev/null || systemctl start ipv6-route
    
    print_success "Serviços reiniciados"
    
    echo ""
    check_status
}

uninstall() {
    print_header "🗑️ Desinstalar Configuração"
    
    check_root
    
    echo -e "${YELLOW}Isso irá remover toda a configuração de rotação IPv6.${NC}"
    read -p "Tem certeza? [y/N]: " confirm
    
    if [[ ! $confirm =~ ^[Yy]$ ]]; then
        print_warning "Desinstalação cancelada"
        exit 0
    fi
    
    local current_block=$(get_current_ipv6_block)
    
    print_info "Parando serviços..."
    systemctl stop ndppd 2>/dev/null || true
    systemctl stop ipv6-route 2>/dev/null || true
    systemctl disable ipv6-route 2>/dev/null || true
    
    print_info "Removendo rota local..."
    if [[ -n "$current_block" ]]; then
        local prefix=$(extract_prefix "$current_block")
        ip -6 route del local ${prefix}::/64 dev lo 2>/dev/null || true
    fi
    
    print_info "Removendo arquivos de configuração..."
    rm -f "$SYSTEMD_SERVICE"
    rm -f "$SYSCTL_FILE"
    # Não removemos ndppd.conf para manter o ndppd instalado
    
    systemctl daemon-reload
    
    print_success "Configuração removida"
    echo ""
    print_info "O pacote ndppd não foi removido. Para remover: apt remove ndppd"
}

show_config_example() {
    print_header "📄 Exemplo de Configuração"
    
    local current_block=$(get_current_ipv6_block)
    local block="${current_block:-XXXX:XXXX:XXXX:XXXX::/64}"
    
    echo -e "${CYAN}application.yml (seção relevante):${NC}"
    echo ""
    cat << EOF
lavalink:
  server:
    sources:
      youtube: false  # IMPORTANTE!
    ratelimit:
      ipBlocks:
        - "${block}"
      strategy: "RotatingNanoSwitch"
      searchTriggersFail: true
      retryLimit: -1

plugins:
  youtube:
    enabled: true
    clients:
      - ANDROID_VR
      - MUSIC
      - TVHTML5EMBEDDED
      - TV
    oauth:
      enabled: true
      refreshToken: "SEU_REFRESH_TOKEN"
EOF
    
    echo ""
    echo -e "${CYAN}Comando para iniciar Lavalink:${NC}"
    echo ""
    echo "java -Djava.net.preferIPv6Addresses=true \\"
    echo "     -Djava.net.preferIPv4Stack=false \\"
    echo "     -Xmx2G \\"
    echo "     -jar Lavalink.jar"
}

# ============================================
# Menu Principal
# ============================================

show_menu() {
    clear
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   🌐 Lavalink IPv6 Rotation Setup          ║${NC}"
    echo -e "${CYAN}╠════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                                            ║${NC}"
    echo -e "${CYAN}║  ${NC}1)${CYAN} Instalação Completa (primeira vez)     ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}2)${CYAN} Atualizar Bloco IPv6                   ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}3)${CYAN} Verificar Status                       ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}4)${CYAN} Reiniciar Serviços                     ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}5)${CYAN} Ver Exemplo de Configuração            ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}6)${CYAN} Desinstalar                            ${CYAN}║${NC}"
    echo -e "${CYAN}║  ${NC}0)${CYAN} Sair                                   ${CYAN}║${NC}"
    echo -e "${CYAN}║                                            ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
    echo ""
}

main() {
    # Se argumentos foram passados, executa direto
    case "$1" in
        install|--install|-i)
            full_install
            exit 0
            ;;
        update|--update|-u)
            update_block
            exit 0
            ;;
        status|--status|-s)
            check_status
            exit 0
            ;;
        restart|--restart|-r)
            restart_services
            exit 0
            ;;
        config|--config|-c)
            show_config_example
            exit 0
            ;;
        uninstall|--uninstall)
            uninstall
            exit 0
            ;;
        help|--help|-h)
            echo "Uso: $0 [comando]"
            echo ""
            echo "Comandos:"
            echo "  install    Instalação completa (primeira vez)"
            echo "  update     Atualizar bloco IPv6"
            echo "  status     Verificar status"
            echo "  restart    Reiniciar serviços"
            echo "  config     Ver exemplo de configuração"
            echo "  uninstall  Desinstalar configuração"
            echo ""
            echo "Sem argumentos: abre menu interativo"
            exit 0
            ;;
    esac
    
    # Menu interativo
    while true; do
        show_menu
        read -p "Escolha uma opção: " choice
        
        case $choice in
            1) full_install ;;
            2) update_block ;;
            3) check_status ;;
            4) restart_services ;;
            5) show_config_example ;;
            6) uninstall ;;
            0) 
                echo ""
                print_info "Até mais! 👋"
                exit 0
                ;;
            *)
                print_error "Opção inválida"
                ;;
        esac
        
        echo ""
        read -p "Pressione Enter para continuar..."
    done
}

# Executar
main "$@"
