import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import "@/App.css";
import { AuthProvider } from "@/context/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";

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

function App() {
    return (
        <div className="App">
            <BrowserRouter>
                <AuthProvider>
                    <Routes>
                        <Route path="/login" element={<Login />} />
                        <Route path="/register" element={<Register />} />
                        <Route path="/" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
                        <Route path="/signals" element={<ProtectedRoute><Signals /></ProtectedRoute>} />
                        <Route path="/bot" element={<ProtectedRoute><BotConfig /></ProtectedRoute>} />
                        <Route path="/accounts" element={<ProtectedRoute><Accounts /></ProtectedRoute>} />
                        <Route path="/trades" element={<ProtectedRoute><Trades /></ProtectedRoute>} />
                        <Route path="/symbols" element={<ProtectedRoute><Symbols /></ProtectedRoute>} />
                        <Route path="/commander" element={<ProtectedRoute><RiskCommander /></ProtectedRoute>} />
                        <Route path="/subscription" element={<ProtectedRoute><Subscription /></ProtectedRoute>} />
                        <Route path="/subscription/success" element={<ProtectedRoute><SubscriptionSuccess /></ProtectedRoute>} />
                        <Route path="/branding" element={<ProtectedRoute><BrandingGallery /></ProtectedRoute>} />
                        <Route path="/affiliate" element={<ProtectedRoute><Affiliate /></ProtectedRoute>} />
                    </Routes>
                    <Toaster theme="dark" position="top-right" />
                </AuthProvider>
            </BrowserRouter>
        </div>
    );
}

export default App;
