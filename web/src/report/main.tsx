import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { readReportData, ReportApp } from "./ReportApp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReportApp data={readReportData(document)} />
  </StrictMode>,
);
