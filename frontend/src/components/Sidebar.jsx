import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import {
    LineChart as ChartLineUp, Activity, Sliders, Wallet, ListChecks,
    DollarSign as CurrencyCircleDollar, LogOut as SignOut, Zap as Lightning
} from "lucide-react";

const items = [
    { to: "/", label: "Dashboard", icon: ChartLineUp, testid: "nav-dashboard" },
    { to: "/signals", label: "AI Signals", icon: Activity, testid: "nav-signals" },
    { to: "/bot", label: "Bot Config", icon: Sliders, testid: "nav-bot" },
    { to: "/accounts", label: "MT5 Accounts", icon: Wallet, testid: "nav-accounts" },
    { to: "/trades", label: "Trades", icon: ListChecks, testid: "nav-trades" },
    { to: "/symbols", label: "Symbols", icon: CurrencyCircleDollar, testid: "nav-symbols" },
];

export function Sidebar({ onNavigate }) {
    const { user, logout } = useAuth();
    const navigate = useNavigate();
    const handleLogout = async () => { await logout(); navigate("/login"); };

    return (
        <aside className="w-full md:w-60 md:h-screen bg-[#0A0A0A] border-r border-[#1F1F1F] flex md:flex-col flex-row md:fixed md:left-0 md:top-0 z-30">
            <div className="p-5 border-b border-[#1F1F1F] hidden md:block">
                <div className="flex items-center gap-2">
                    <div className="w-7 h-7 bg-[#00FF41] flex items-center justify-center">
                        <Lightning className="w-4 h-4 text-black" />
                    </div>
                    <div>
                        <div className="font-display font-bold text-sm tracking-tight">EMERGENT</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">AI TRADING v1.0</div>
                    </div>
                </div>
            </div>

            <nav className="flex md:flex-col flex-row md:p-2 p-1 gap-0.5 flex-1 overflow-x-auto md:overflow-x-visible">
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
            </nav>

            <div className="hidden md:block p-3 border-t border-[#1F1F1F]">
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
