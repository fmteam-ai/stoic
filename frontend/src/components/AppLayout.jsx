import { Sidebar } from "@/components/Sidebar";
import { CoPilotWidget } from "@/components/CoPilotWidget";
import { SubscriptionBanner } from "@/components/SubscriptionBanner";
import { TickerTape } from "@/components/TickerTape";
import { StatusBar } from "@/components/StatusBar";

export function AppLayout({ children }) {
    return (
        <div className="min-h-screen bg-[#050505] text-white">
            <Sidebar />
            <main className="md:ml-60 min-h-screen">
                <StatusBar />
                <TickerTape />
                <SubscriptionBanner />
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
