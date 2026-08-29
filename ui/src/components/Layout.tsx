import type { ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { ReviewerIdentityControl } from "./ReviewerIdentity";

const NAV_ITEMS = [
  { to: "/", label: "Overview", end: true },
  { to: "/workflows", label: "Workflows" },
  { to: "/signals", label: "Signals" },
  { to: "/detections", label: "Detections" },
  { to: "/feature-reviews", label: "Feature Reviews" },
  { to: "/resolutions", label: "Incidents" },
  { to: "/verifications", label: "Verifications" },
];

export function Layout() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            Q
          </span>
          <div>
            <div className="brand-title">Quipu Control Plane</div>
            <div className="brand-subtitle">Detect → Decide → Execute → Verify</div>
          </div>
        </div>
        <nav className="app-nav" aria-label="Primary">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <ReviewerIdentityControl />
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Panel({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="panel">
      {(title || actions) && (
        <div className="panel-header">
          {title && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}
