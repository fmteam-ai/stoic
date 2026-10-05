import { Sidebar } from "@/components/Sidebar";
import { CoPilotWidget } from "@/components/CoPilotWidget";
import { SubscriptionBanner } from "@/components/SubscriptionBanner";
import { TickerTape } from "@/components/TickerTape";
import { StatusBar } from "@/components/StatusBar";
import { AuthorityStrip } from "@/components/AuthorityStrip";
import { QuickActionsBar } from "@/components/QuickActionsBar";
import { OnboardingBanner } from "@/components/OnboardingBanner";
import OnboardingWizard from "@/components/OnboardingWizard";
import { StepUpDialog } from "@/components/StepUpDialog";
import { TourRunner } from "@/components/GuidedTour";
import { TradingReadinessStrip } from "@/components/TradingReadinessStrip";
import { SecurityCriticalBanner } from "@/components/security/SecurityCriticalBanner";
import { useEffect } from "react";
import { useLocation } from "react-router-dom";

// audit F-20 — route-specific document titles
const TITLES = {
    "/": "Dashboard", "/dashboard": "Dashboard", "/trades": "Trades",
    "/signals": "AI Signals", "/commander": "Risk Commander",
    "/bot-health": "Bot Health", "/accounts": "MT5 Accounts",
    "/bot": "Bot Config", "/bot-config": "Bot Config",
    "/certification": "Certification", "/certification-center": "Certification Center",
    "/scalp": "Scalp Fast Path", "/execution": "Execution Intel",
    "/verified-performance": "Verified Performance", "/audit-log": "Audit Log",
    "/loss-lab": "Loss Lab", "/portfolio": "Portfolio Risk",
    "/connect": "STOIC Connect", "/strategies": "Strategies",
    "/safety-blocks": "Safety Blocks", "/infrastructure": "Infrastructure",
    "/agents": "AI Agents", "/research": "Research",
    "/marketplace": "Marketplace", "/vps": "VPS", "/analytics": "Analytics",
    "/subscription": "Subscription", "/settings": "Settings",
    "/notifications": "Notifications", "/help": "Help", "/support": "Support",
    "/status": "System Status", "/admin/command-center": "Command Center",
};

export function AppLayout({ children }) {
    const location = useLocation();
    useEffect(() => {
        const name = TITLES[location.pathname]
            || (location.pathname.split("/").filter(Boolean)[0] || "")
                .replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
        document.title = name ? `${name} | STOIC` : "STOIC · Disciplined AI Trading";
    }, [location.pathname]);
    return (
        <div className="min-h-screen bg-[#050505] text-white">
            <Sidebar />
            <QuickActionsBar />
            <StepUpDialog />
            <TourRunner />
            <main className="md:ml-60 min-h-screen">
                <TradingReadinessStrip />
                <StatusBar />
                <AuthorityStrip />
                <TickerTape />
                <SubscriptionBanner />
                <SecurityCriticalBanner />
                <div className="px-4 md:px-8 pt-3">
                    <OnboardingBanner />
                    <OnboardingWizard />
                </div>
                {children}
            </main>
            <CoPilotWidget />
        </div>
    );
}

export function PageHeader({ title, subtitle, action, testid }) {
    return (
        <div className="border-b border-[#1F1F1F] px-4 md:px-8 py-5 flex items-start justify-between gap-4 flex-wrap" data-testid={testid}>
            <div>
                <h1 className="font-display font-bold text-2xl md:text-3xl tracking-tight">{title}</h1>
                {subtitle && <p className="text-sm text-[#A1A1AA] mt-1">{subtitle}</p>}
            </div>
            {action}
        </div>
    );
}

export function Section({ children, className = "" }) {
    return (
        <section className={`border-b border-[#1F1F1F] ${className}`}>{children}</section>
    );
}
