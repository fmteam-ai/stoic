import { NavLink, useNavigate } from "react-router-dom";
import { useEffect, useState, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";
import { StoicMark } from "@/components/StoicLogo";
import api from "@/lib/api";
import {
    LineChart as ChartLineUp, Activity, Sliders, Wallet, ListChecks,
    DollarSign as CurrencyCircleDollar, LogOut as SignOut,
    MessageSquare, Sparkles, Users, Bell, BarChart3, Settings as SettingsIcon,
    CreditCard, HelpCircle, BookOpen, Cpu, LifeBuoy, ExternalLink, ShieldCheck,
    Shield, Zap, Brain, Bitcoin, Eye, FlaskConical, Stethoscope, ChevronDown, ChevronRight, Layers,
    FileText, ShieldAlert, DatabaseBackup,
} from "lucide-react";

const SUPPORT_TELEGRAM_URL = "https://t.me/+rhr2qxcNW90zYjg0";

// Simple-Mode whitelist — the only 5 pages a new user needs.
// `Bot Health` is included so the user always has eyes on the bot.
const SIMPLE_MODE_ROUTES = new Set([
    "/", "/bot-health", "/bot", "/trades", "/settings",
]);

// Grouped sidebar layout (Pro Mode). Each section is collapsible.
// Order optimised for usage frequency — Trading first, Account/Learn last.
const SECTIONS = [
    {
        key: "trading",
        label: "TRADING",
        items: [
            { to: "/", label: "Dashboard", icon: ChartLineUp, testid: "nav-dashboard" },
            { to: "/trades", label: "Trades", icon: ListChecks, testid: "nav-trades" },
            { to: "/signals", label: "AI Signals", icon: Activity, testid: "nav-signals" },
            { to: "/commander", label: "Risk Commander", icon: MessageSquare, testid: "nav-commander" },
        ],
    },
    {
        key: "insights",
        label: "INSIGHTS",
        items: [
            { to: "/bot-health", label: "Bot Health", icon: Stethoscope, testid: "nav-bot-health" },
            { to: "/analytics", label: "Analytics", icon: BarChart3, testid: "nav-analytics" },
            { to: "/loss-lab", label: "Loss Lab", icon: FlaskConical, testid: "nav-loss-lab" },
            { to: "/shadow-performance", label: "Shadow Report", icon: Eye, testid: "nav-shadow" },
        ],
    },
    {
        key: "automation",
        label: "AUTOMATION",
        items: [
            { to: "/bot", label: "Bot Config", icon: Sliders, testid: "nav-bot" },
            { to: "/strategies", label: "Strategies", icon: Sparkles, testid: "nav-strategies" },
            { to: "/agents", label: "Agents", icon: Cpu, testid: "nav-agents" },
            { to: "/research", label: "Research Agent", icon: Brain, testid: "nav-research" },
        ],
    },
    {
        key: "infra",
        label: "INFRASTRUCTURE",
        items: [
            { to: "/accounts", label: "MT5 Accounts", icon: Wallet, testid: "nav-accounts" },
            { to: "/crypto", label: "Crypto · Binance", icon: Bitcoin, testid: "nav-crypto" },
            { to: "/portfolio", label: "Portfolio Risk", icon: Shield, testid: "nav-portfolio" },
            { to: "/safety-blocks", label: "Safety Blocks", icon: ShieldCheck, testid: "nav-safety-blocks" },
            { to: "/execution", label: "Execution Intel", icon: Zap, testid: "nav-execution" },
            { to: "/symbols", label: "Symbols", icon: CurrencyCircleDollar, testid: "nav-symbols" },
        ],
    },
    {
        key: "account",
        label: "ACCOUNT",
        items: [
            { to: "/notifications", label: "Notifications", icon: Bell, testid: "nav-notifications" },
            { to: "/subscription", label: "Subscription", icon: Sparkles, testid: "nav-subscription" },
            { to: "/billing", label: "Billing", icon: CreditCard, testid: "nav-billing" },
            { to: "/affiliate", label: "Affiliate", icon: Users, testid: "nav-affiliate" },
            { to: "/settings", label: "Settings", icon: SettingsIcon, testid: "nav-settings" },
        ],
    },
    {
        key: "learn",
        label: "LEARN",
        items: [
            { to: "/guide", label: "Guide", icon: BookOpen, testid: "nav-guide" },
            { to: "/faq", label: "FAQ", icon: HelpCircle, testid: "nav-faq" },
            { to: "/terms", label: "Terms of Use", icon: FileText, testid: "nav-terms" },
        ],
    },
];

// Admin-only section — appended dynamically when user.role === 'admin'.
const ADMIN_SECTION = {
    key: "admin",
    label: "ADMIN",
    items: [
        { to: "/admin/users", label: "User Management", icon: Users, testid: "nav-admin-users" },
        { to: "/admin/affiliates", label: "Affiliate Mgmt", icon: ShieldAlert, testid: "nav-admin-affiliates" },
        { to: "/admin/migration", label: "Migration", icon: DatabaseBackup, testid: "nav-admin-migration" },
    ],
};

// Default-open: TRADING + INSIGHTS (the daily-use sections).
const DEFAULT_OPEN = new Set(["trading", "insights"]);

function SectionGroup({ section, openMap, toggle, onNavigate }) {
    const open = openMap[section.key] !== false;  // open unless explicitly closed
    return (
        <div className="md:mb-1">
            <button
                onClick={() => toggle(section.key)}
                className="hidden md:flex w-full items-center gap-2 px-3 py-1.5 text-[10px] font-mono tracking-widest text-[#52525B] hover:text-[#A1A1AA] transition-colors"
                data-testid={`sidebar-section-${section.key}`}>
                {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                {section.label}
            </button>
            {open && section.items.map(it => (
                <NavLink
                    key={it.to} to={it.to} end={it.to === "/"} onClick={onNavigate}
                    data-testid={it.testid}
                    className={({ isActive }) => `flex items-center gap-2 px-3 py-2 text-sm transition-colors duration-150 whitespace-nowrap ${
                        isActive
                            ? "bg-[#121212] text-white border-l-2 border-[#00FF41] md:border-l-2 border-b-2 md:border-b-0"
                            : "text-[#A1A1AA] hover:text-white hover:bg-[#121212] border-l-2 border-transparent md:border-l-2 border-b-2 md:border-b-0 border-b-transparent"
                    }`}>
                    <it.icon className="w-4 h-4 shrink-0" />
                    <span>{it.label}</span>
                </NavLink>
            ))}
        </div>
    );
}

export function Sidebar({ onNavigate }) {
    const { user, logout } = useAuth();
    const navigate = useNavigate();
    // Simple-Mode preference is per-user, stored on the user doc + localStorage cache.
    const [simpleMode, setSimpleMode] = useState(() => {
        try {
            return localStorage.getItem("stoic_simple_mode") === "true";
        } catch { return false; }
    });
    const [openMap, setOpenMap] = useState(() => {
        try {
            // iter-98: bumped storage key from `stoic_sidebar_open` → `_v2`
            // so any user who had collapsed the INSIGHTS group (hiding Bot
            // Health) gets a fresh default with all daily-use sections open.
            const stored = JSON.parse(localStorage.getItem("stoic_sidebar_open_v2") || "{}");
            // Default the daily-use sections to open if no preference saved
            return { ...Object.fromEntries([...DEFAULT_OPEN].map(k => [k, true])), ...stored };
        } catch { return Object.fromEntries([...DEFAULT_OPEN].map(k => [k, true])); }
    });

    // Sync simple-mode preference from server on mount
    useEffect(() => {
        api.get("/settings/preferences")
            .then(r => {
                const v = !!r.data?.simple_mode;
                setSimpleMode(v);
                try { localStorage.setItem("stoic_simple_mode", String(v)); } catch { /* ignore */ }
            })
            .catch(() => { /* preference endpoint may not exist yet */ });
    }, []);

    const toggleSimpleMode = async () => {
        const next = !simpleMode;
        setSimpleMode(next);
        try { localStorage.setItem("stoic_simple_mode", String(next)); } catch { /* ignore */ }
        api.post("/settings/preferences", { simple_mode: next }).catch(() => { /* ok */ });
    };

    const toggle = useCallback((key) => {
        setOpenMap(m => {
            const next = { ...m, [key]: !(m[key] !== false) };
            try { localStorage.setItem("stoic_sidebar_open_v2", JSON.stringify(next)); } catch { /* ignore */ }
            return next;
        });
    }, []);

    const handleLogout = async () => { await logout(); navigate("/login"); };

    // In Simple Mode, flatten to the 5 essentials in fixed order.
    const visibleSections = simpleMode
        ? [{
            key: "simple", label: "ESSENTIALS",
            items: SECTIONS.flatMap(s => s.items).filter(it => SIMPLE_MODE_ROUTES.has(it.to))
                .sort((a, b) => [...SIMPLE_MODE_ROUTES].indexOf(a.to) - [...SIMPLE_MODE_ROUTES].indexOf(b.to)),
          }]
        : (user?.role === "admin" ? [...SECTIONS, ADMIN_SECTION] : SECTIONS);

    return (
        <aside className="w-full md:w-60 md:h-screen bg-[#0A0A0A] border-r border-[#1F1F1F] flex md:flex-col flex-row md:fixed md:left-0 md:top-0 z-30">
            <div className="p-5 border-b border-[#1F1F1F] hidden md:block shrink-0">
                <div className="flex items-center gap-2.5">
                    <StoicMark size={36} />
                    <div>
                        <div className="font-display font-bold text-sm tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[9px] text-[#52525B] tracking-widest">STEADY WEALTH · v1.0</div>
                    </div>
                </div>
                <button onClick={toggleSimpleMode}
                    data-testid="sidebar-simple-mode-toggle"
                    title={simpleMode ? "Switch to Pro Mode" : "Switch to Simple Mode"}
                    className={`mt-3 w-full flex items-center justify-center gap-1.5 px-2 py-1 border text-[10px] font-mono tracking-widest transition-colors ${
                        simpleMode
                            ? "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]"
                            : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"
                    }`}>
                    <Layers className="w-3 h-3" />
                    {simpleMode ? "SIMPLE MODE" : "PRO MODE"}
                </button>
            </div>

            <nav className="stoic-sidebar-scroll flex md:flex-col flex-row md:p-2 p-1 gap-0.5 flex-1 min-h-0 overflow-x-auto md:overflow-x-visible md:overflow-y-auto">
                {visibleSections.map(s => (
                    <SectionGroup key={s.key} section={s} openMap={openMap} toggle={toggle} onNavigate={onNavigate} />
                ))}
                <a href={SUPPORT_TELEGRAM_URL}
                    target="_blank" rel="noopener noreferrer"
                    onClick={onNavigate}
                    data-testid="nav-support-telegram"
                    className="flex items-center gap-2 px-3 py-2 text-sm transition-colors duration-150 whitespace-nowrap shrink-0 md:whitespace-normal text-[#00FF41] hover:bg-[#00FF41]/10 border-l-2 border-transparent md:border-l-2 border-b-2 md:border-b-0 border-b-transparent">
                    <LifeBuoy className="w-4 h-4 shrink-0" />
                    <span>Support</span>
                    <ExternalLink className="w-3 h-3 ml-auto opacity-60 hidden md:inline" />
                </a>
            </nav>

            <div className="hidden md:block p-3 border-t border-[#1F1F1F] shrink-0">
                <div className="px-2 py-2 mb-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">SIGNED IN AS</div>
                    <div className="text-sm truncate text-white" data-testid="sidebar-user-email">{user?.email}</div>
                </div>
                <button onClick={handleLogout} data-testid="logout-button"
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[#A1A1AA] hover:text-white hover:bg-[#121212] transition-colors duration-150">
                    <SignOut className="w-4 h-4" />
                    <span>Sign out</span>
                </button>
            </div>
        </aside>
    );
}
