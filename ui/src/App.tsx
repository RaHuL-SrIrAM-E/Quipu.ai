import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { Workflows } from "./pages/Workflows";
import { WorkflowDetail } from "./pages/WorkflowDetail";
import { Signals } from "./pages/Signals";
import { Detections } from "./pages/Detections";
import { FeatureReviews } from "./pages/FeatureReviews";
import { Resolutions } from "./pages/Resolutions";
import { ResolutionDetail } from "./pages/ResolutionDetail";
import { Verifications } from "./pages/Verifications";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/workflows" element={<Workflows />} />
        <Route path="/workflows/:workflowId" element={<WorkflowDetail />} />
        <Route path="/signals" element={<Signals />} />
        <Route path="/detections" element={<Detections />} />
        <Route path="/feature-reviews" element={<FeatureReviews />} />
        <Route path="/resolutions" element={<Resolutions />} />
        <Route path="/resolutions/:resolutionId" element={<ResolutionDetail />} />
        <Route path="/verifications" element={<Verifications />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
