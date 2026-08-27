import { lazy, Suspense } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import "@/App.css";
import { AuthProvider } from "@/context/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SiteFooter } from "@/components/SiteFooter";

import Login from "@/pages/Login";
import Register from "@/pages/Register";
import Dashboard from "@/pages/Dashboard";
import Signals from "@/pages/Signals";
import BotConfig from "@/pages/BotConfig";
import Accounts from "@/pages/Accounts";
import Trades from "@/pages/Trades";
import Symbols from "@/pages/Symbols";
import RiskCommander from "@/pages/RiskCommander";
import Subscription from "@/pages/Subscription";
import SubscriptionSuccess from "@/pages/SubscriptionSuccess";
import BrandingGallery from "@/pages/BrandingGallery";
import Affiliate from "@/pages/Affiliate";
import Notifications from "@/pages/Notifications";
import Settings from "@/pages/Settings";
import FAQ from "@/pages/FAQ";
import Guide from "@/pages/Guide";
import SafetyBlocks from "@/pages/SafetyBlocks";
import Agents from "@/pages/Agents";
import Strategies from "@/pages/Strategies";
import Portfolio from "@/pages/Portfolio";
import VerifiedPerformance from "@/pages/VerifiedPerformance";
import AuditLog from "@/pages/AuditLog";
import Execution from "@/pages/Execution";
import Crypto from "@/pages/Crypto";
import ShadowPerformance from "@/pages/ShadowPerformance";
import Scoreboard from "@/pages/Scoreboard";
import Marketplace from "@/pages/Marketplace";
import BrokerComparison from "@/pages/BrokerComparison";
import Scalp from "@/pages/Scalp";
import LossLab from "@/pages/LossLab";
import BotHealth from "@/pages/BotHealth";
import AffiliateLanding from "@/pages/AffiliateLanding";
import WelcomeTrailer from "@/pages/WelcomeTrailer";
import Terms from "@/pages/Terms";
import VerifyEmail from "@/pages/VerifyEmail";
import ForgotPassword from "@/pages/ForgotPassword";
import ResetPassword from "@/pages/ResetPassword";

// Route-level code splitting — heavy/rarely-visited surfaces load on demand
const Research = lazy(() => import("@/pages/Research"));
const Analytics = lazy(() => import("@/pages/Analytics"));
const Infrastructure = lazy(() => import("@/pages/Infrastructure"));
const Billing = lazy(() => import("@/pages/Billing"));
const EnterpriseApi = lazy(() => import("@/pages/EnterpriseApi"));
const PammGuide = lazy(() => import("@/pages/PammGuide"));
const PublicPerformance = lazy(() => import("@/pages/PublicPerformance"));
const PublicJournal = lazy(() => import("@/pages/PublicJournal"));
const AdminUsers = lazy(() => import("@/pages/AdminUsers"));
const AdminAffiliates = lazy(() => import("@/pages/AdminAffiliates"));
const AdminMigration = lazy(() => import("@/pages/AdminMigration"));
const AdminSupport = lazy(() => import("@/pages/AdminSupport"));
const AdminOps = lazy(() => import("@/pages/AdminOps"));
const AdminBrokers = lazy(() => import("@/pages/AdminBrokers"));
const AdminRunbooks = lazy(() => import("@/pages/AdminRunbooks"));
const DeployPreflight = lazy(() => import("@/pages/DeployPreflight"));
const ManagedStrategy = lazy(() => import("@/pages/ManagedStrategy"));
const ComingSoon = lazy(() => import("@/pages/ComingSoon"));
const HelpCenter = lazy(() => import("@/pages/HelpCenter"));
const Support = lazy(() => import("@/pages/Support"));
const StatusPage = lazy(() => import("@/pages/StatusPage"));
const Legal = lazy(() => import("@/pages/Legal"));

const RouteFallback = () => (
    <div className="min-h-screen bg-[#050505] flex items-center justify-center"
        data-testid="route-loading">
        <span className="font-mono text-[10px] tracking-[0.3em] text-[#52525B]">LOADING…</span>
    </div>
);


function App() {
    return (
        <div className="App">
            <ErrorBoundary>
            <BrowserRouter>
                <AuthProvider>
                    <Suspense fallback={<RouteFallback />}>
                    <Routes>
                        <Route path="/login" element={<Login />} />
                        <Route path="/register" element={<Register />} />
                        {/* Public marketing trailer — no auth required */}
                        <Route path="/welcome" element={<WelcomeTrailer />} />
                        {/* Public marketing TOS — must be reachable pre-login */}
                        <Route path="/terms" element={<Terms />} />
                        <Route path="/privacy" element={<Legal kind="privacy" />} />
                        <Route path="/risk-disclosure" element={<Legal kind="risk" />} />
                        <Route path="/status" element={<StatusPage />} />
                        {/* Public email-verification landing page */}
                        <Route path="/verify-email" element={<VerifyEmail />} />
                        {/* Public password-reset flow */}
                        <Route path="/forgot-password" element={<ForgotPassword />} />
                        <Route path="/reset-password" element={<ResetPassword />} />
                        {/* Public affiliate marketing page — no auth required */}
                        <Route path="/affiliates" element={<AffiliateLanding />} />
                        {/* Admin-only moderation console */}
                        <Route path="/admin/users" element={<ProtectedRoute requireAdmin><AdminUsers /></ProtectedRoute>} />
                        <Route path="/admin/affiliates" element={<ProtectedRoute requireAdmin><AdminAffiliates /></ProtectedRoute>} />
                        <Route path="/admin/migration" element={<ProtectedRoute requireAdmin><AdminMigration /></ProtectedRoute>} />
                        <Route path="/admin/ops" element={<ProtectedRoute requireAdmin><AdminOps /></ProtectedRoute>} />
                        <Route path="/admin/brokers" element={<ProtectedRoute requireAdmin><AdminBrokers /></ProtectedRoute>} />
                        <Route path="/admin/support" element={<ProtectedRoute requireAdmin><AdminSupport /></ProtectedRoute>} />
                        <Route path="/admin/runbooks" element={<ProtectedRoute requireAdmin><AdminRunbooks /></ProtectedRoute>} />
                        <Route path="/admin/preflight" element={<ProtectedRoute requireAdmin><DeployPreflight /></ProtectedRoute>} />
                        <Route path="/help" element={<ProtectedRoute><HelpCenter /></ProtectedRoute>} />
                        <Route path="/support" element={<ProtectedRoute><Support /></ProtectedRoute>} />
                        <Route path="/" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
                        <Route path="/signals" element={<ProtectedRoute><Signals /></ProtectedRoute>} />
                        <Route path="/bot" element={<ProtectedRoute><BotConfig /></ProtectedRoute>} />
                        {/* Alias — `bot-config` is the documented path, while
                            the historical sidebar link is `/bot`. Both render
                            the same page so direct navigation works either way. */}
                        <Route path="/bot-config" element={<ProtectedRoute><BotConfig /></ProtectedRoute>} />
                        <Route path="/accounts" element={<ProtectedRoute><Accounts /></ProtectedRoute>} />
                        <Route path="/crypto" element={<ProtectedRoute><Crypto /></ProtectedRoute>} />
                        <Route path="/shadow-performance" element={<ProtectedRoute><ShadowPerformance /></ProtectedRoute>} />
                        <Route path="/performance" element={<ProtectedRoute><VerifiedPerformance /></ProtectedRoute>} />
                        <Route path="/audit-log" element={<ProtectedRoute><AuditLog /></ProtectedRoute>} />
                        <Route path="/p/:shareId" element={<PublicPerformance />} />
                        <Route path="/j/:shareId" element={<PublicJournal />} />
                        <Route path="/scoreboard" element={<ProtectedRoute><Scoreboard /></ProtectedRoute>} />
                        <Route path="/marketplace" element={<ProtectedRoute><Marketplace /></ProtectedRoute>} />
                        <Route path="/managed" element={<ProtectedRoute requireAdmin><ManagedStrategy /></ProtectedRoute>} />
                        <Route path="/pamm-guide" element={<ProtectedRoute><PammGuide /></ProtectedRoute>} />
                        <Route path="/vps" element={<ProtectedRoute><ComingSoon title="VPS Hosting" subtitle="Low-latency trading VPS, managed by STOIC." blurb="Dedicated low-latency VPS instances co-located near broker servers, with one-click EA deployment and 24/7 uptime monitoring. This product is on the roadmap." testid="vps-page" /></ProtectedRoute>} />
                        <Route path="/brokers" element={<ProtectedRoute><BrokerComparison /></ProtectedRoute>} />
                        <Route path="/enterprise-api" element={<ProtectedRoute><EnterpriseApi /></ProtectedRoute>} />
                        <Route path="/scalp" element={<ProtectedRoute><Scalp /></ProtectedRoute>} />
                        <Route path="/trades" element={<ProtectedRoute><Trades /></ProtectedRoute>} />
                        <Route path="/loss-lab" element={<ProtectedRoute><LossLab /></ProtectedRoute>} />
                        <Route path="/bot-health" element={<ProtectedRoute><BotHealth /></ProtectedRoute>} />
                        <Route path="/safety-blocks" element={<ProtectedRoute><SafetyBlocks /></ProtectedRoute>} />
                        <Route path="/symbols" element={<ProtectedRoute><Symbols /></ProtectedRoute>} />
                        <Route path="/commander" element={<ProtectedRoute><RiskCommander /></ProtectedRoute>} />
                        <Route path="/risk-commander" element={<Navigate to="/commander" replace />} />
                        <Route path="/subscription" element={<ProtectedRoute><Subscription /></ProtectedRoute>} />
                        <Route path="/subscription/success" element={<ProtectedRoute><SubscriptionSuccess /></ProtectedRoute>} />
                        <Route path="/branding" element={<ProtectedRoute><BrandingGallery /></ProtectedRoute>} />
                        <Route path="/affiliate" element={<ProtectedRoute><Affiliate /></ProtectedRoute>} />
                        <Route path="/notifications" element={<ProtectedRoute><Notifications /></ProtectedRoute>} />
                        <Route path="/analytics" element={<ProtectedRoute><Analytics /></ProtectedRoute>} />
                        <Route path="/infrastructure" element={<ProtectedRoute><Infrastructure /></ProtectedRoute>} />
                        <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
                        <Route path="/billing" element={<ProtectedRoute><Billing /></ProtectedRoute>} />
                        <Route path="/faq" element={<ProtectedRoute><FAQ /></ProtectedRoute>} />
                        <Route path="/guide" element={<ProtectedRoute><Guide /></ProtectedRoute>} />
                        <Route path="/agents" element={<ProtectedRoute><Agents /></ProtectedRoute>} />
                        <Route path="/strategies" element={<ProtectedRoute><Strategies /></ProtectedRoute>} />
                        <Route path="/portfolio" element={<ProtectedRoute><Portfolio /></ProtectedRoute>} />
                        <Route path="/execution" element={<ProtectedRoute><Execution /></ProtectedRoute>} />
                        <Route path="/research" element={<ProtectedRoute><Research /></ProtectedRoute>} />
                        <Route path="*" element={<Navigate to="/" replace />} />
                    </Routes>
                    </Suspense>
                    <SiteFooter />
                    <Toaster theme="dark" position="top-right" />
                </AuthProvider>
            </BrowserRouter>
            </ErrorBoundary>
        </div>
    );
}

export default App;
