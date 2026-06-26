import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { StoicMark } from "@/components/StoicLogo";
import {
    LineChart as ChartLineUp, Activity, Sliders, Wallet, ListChecks,
    DollarSign as CurrencyCircleDollar, LogOut as SignOut,
    MessageSquare, Sparkles, Users, Bell, BarChart3, Settings as SettingsIcon,
    CreditCard, HelpCircle, BookOpen, Cpu, LifeBuoy, ExternalLink, ShieldCheck, Shield, Zap, Brain, Bitcoin, Eye, FlaskConical
} from "lucide-react";

const SUPPORT_TELEGRAM_URL = "https://t.me/+rhr2qxcNW90zYjg0";

const items = [
    { to: "/", label: "Dashboard", icon: ChartLineUp, testid: "nav-dashboard" },
    { to: "/signals", label: "AI Signals", icon: Activity, testid: "nav-signals" },
    { to: "/agents", label: "Agents", icon: Cpu, testid: "nav-agents" },
    { to: "/strategies", label: "Strategies", icon: Sparkles, testid: "nav-strategies" },
    { to: "/research", label: "Research Agent", icon: Brain, testid: "nav-research" },
    { to: "/commander", label: "Risk Commander", icon: MessageSquare, testid: "nav-commander" },
    { to: "/bot", label: "Bot Config", icon: Sliders, testid: "nav-bot" },
    { to: "/accounts", label: "MT5 Accounts", icon: Wallet, testid: "nav-accounts" },
    { to: "/crypto", label: "Crypto · Binance", icon: Bitcoin, testid: "nav-crypto" },
    { to: "/shadow-performance", label: "Shadow Report", icon: Eye, testid: "nav-shadow" },
    { to: "/trades", label: "Trades", icon: ListChecks, testid: "nav-trades" },
    { to: "/loss-lab", label: "Loss Lab", icon: FlaskConical, testid: "nav-loss-lab" },
    { to: "/safety-blocks", label: "Safety Blocks", icon: ShieldCheck, testid: "nav-safety-blocks" },
    { to: "/portfolio", label: "Portfolio Risk", icon: Shield, testid: "nav-portfolio" },
    { to: "/execution", label: "Execution Intel", icon: Zap, testid: "nav-execution" },
    { to: "/analytics", label: "Analytics", icon: BarChart3, testid: "nav-analytics" },
    { to: "/symbols", label: "Symbols", icon: CurrencyCircleDollar, testid: "nav-symbols" },
    { to: "/notifications", label: "Notifications", icon: Bell, testid: "nav-notifications" },
    { to: "/subscription", label: "Subscription", icon: Sparkles, testid: "nav-subscription" },
    { to: "/billing", label: "Billing", icon: CreditCard, testid: "nav-billing" },
    { to: "/affiliate", label: "Affiliate", icon: Users, testid: "nav-affiliate" },
    { to: "/settings", label: "Settings", icon: SettingsIcon, testid: "nav-settings" },
    { to: "/guide", label: "Guide", icon: BookOpen, testid: "nav-guide" },
    { to: "/faq", label: "FAQ", icon: HelpCircle, testid: "nav-faq" },
];

export function Sidebar({ onNavigate }) {
    const { user, logout } = useAuth();
    const navigate = useNavigate();
    const handleLogout = async () => { await logout(); navigate("/login"); };

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
            </div>

            <nav className="stoic-sidebar-scroll flex md:flex-col flex-row md:p-2 p-1 gap-0.5 flex-1 min-h-0 overflow-x-auto md:overflow-x-visible md:overflow-y-auto">
                {items.map(it => (
                    <NavLink
                        key={it.to}
                        to={it.to}
                        end={it.to === "/"}
                        onClick={onNavigate}
                        data-testid={it.testid}
                        className={({ isActive }) => `flex items-center gap-2 px-3 py-2 text-sm transition-colors duration-150 whitespace-nowrap ${
                            isActive
                                ? "bg-[#121212] text-white border-l-2 border-[#00FF41] md:border-l-2 border-b-2 md:border-b-0"
                                : "text-[#A1A1AA] hover:text-white hover:bg-[#121212] border-l-2 border-transparent md:border-l-2 border-b-2 md:border-b-0 border-b-transparent"
                        }`}
                    >
                        <it.icon className="w-4 h-4 shrink-0" />
                        <span>{it.label}</span>
                    </NavLink>
                ))}

                <a href={SUPPORT_TELEGRAM_URL}
                    target="_blank"
                    rel="noopener noreferrer"
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
                <button
                    onClick={handleLogout}
                    data-testid="logout-button"
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[#A1A1AA] hover:text-white hover:bg-[#121212] transition-colors duration-150"
                >
                    <SignOut className="w-4 h-4" />
                    <span>Sign out</span>
                </button>
            </div>
        </aside>
    );
}
