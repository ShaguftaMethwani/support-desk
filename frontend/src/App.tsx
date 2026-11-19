import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { AuthProvider } from "./auth/AuthProvider";
import { Layout } from "./components/Layout";
import { Approvals } from "./views/Approvals";
import { Chat } from "./views/Chat";
import { Explainers } from "./views/Explainers";
import { Queue } from "./views/Queue";
import { Sources } from "./views/Sources";
import { Weekly } from "./views/Weekly";

export function App() {
  return (
    <BrowserRouter>
      {/* Every view, chat included, is behind the login. */}
      <AuthProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/chat" replace />} />
            <Route path="chat" element={<Chat />} />
            <Route path="queue" element={<Queue />} />
            <Route path="approvals" element={<Approvals />} />
            <Route path="weekly" element={<Weekly />} />
            <Route path="explainers" element={<Explainers />} />
            <Route path="sources" element={<Sources />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
