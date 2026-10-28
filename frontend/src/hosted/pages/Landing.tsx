import { Link } from 'react-router-dom'
import { SiteLayout } from '../SiteLayout'

const COVERS = [
  ['Networks', 'VPCs, subnets, routes, NAT, security groups → VPC network, firewall rules, Cloud NAT'],
  ['Databases', 'RDS MySQL and PostgreSQL → Cloud SQL with private IP, high availability and backups'],
  ['Servers', 'EC2, disks, Elastic IPs, Auto Scaling → Compute Engine and managed instance groups'],
  ['Kubernetes', 'EKS clusters and node groups → GKE Standard clusters and node pools'],
  ['Storage', 'S3 bucket settings → Cloud Storage buckets'],
  ['Load balancers', 'Application and Network Load Balancers → Cloud Load Balancing'],
]

export function Landing() {
  return (
    <SiteLayout>
      <section className="h-hero">
        <p className="h-eyebrow">AWS → Google Cloud</p>
        <h1>See exactly how your AWS setup moves to Google Cloud</h1>
        <p className="h-hero-text">
          EasyMigrate reads your AWS infrastructure, shows every difference between the two clouds
          with a safe choice already made for each, and gives you ready-to-run Terraform for Google
          Cloud. Free, read-only, nothing to install.
        </p>
        <div className="h-hero-actions">
          <Link to="/app" className="h-button">
            Start free
          </Link>
          <Link to="/guide" className="h-button h-button-ghost">
            How it works
          </Link>
        </div>
      </section>

      <section className="h-section">
        <h2>Three steps, about ten minutes</h2>
        <div className="h-grid h-grid-3">
          <div className="h-tile">
            <span className="h-num">1</span>
            <h3>Connect AWS, read-only</h3>
            <p>One click in your own AWS console creates a role that can only read. No passwords or keys.</p>
          </div>
          <div className="h-tile">
            <span className="h-num">2</span>
            <h3>Review the differences</h3>
            <p>Every place AWS and Google Cloud behave differently, explained, with a safe default.</p>
          </div>
          <div className="h-tile">
            <span className="h-num">3</span>
            <h3>Download</h3>
            <p>Terraform for Google Cloud plus a full report. Apply it yourself, whenever you're ready.</p>
          </div>
        </div>
      </section>

      <section className="h-section">
        <h2>What it covers</h2>
        <div className="h-grid h-grid-2">
          {COVERS.map(([title, text]) => (
            <div key={title} className="h-tile h-tile-flat">
              <h3>{title}</h3>
              <p>{text}</p>
            </div>
          ))}
        </div>
        <p className="h-hint">
          Configuration only: EasyMigrate never copies data (database rows, disk contents, bucket
          files). IAM roles get a report with suggested Google Cloud roles.
        </p>
      </section>

      <section className="h-section h-safety">
        <h2>Safe by design</h2>
        <ul>
          <li>
            <strong>Read-only.</strong> The role you create can read configuration and nothing else.
            EasyMigrate never changes your AWS account.
          </li>
          <li>
            <strong>Nothing to hand over.</strong> No access keys, no passwords. Delete the role in
            AWS and access ends.
          </li>
          <li>
            <strong>Nothing kept.</strong> Your scan is encrypted and deleted 7 days after your last
            visit, or the moment you click “Delete my data”.
          </li>
          <li>
            <strong>Nothing created for you.</strong> You get the code; you decide when to apply it.
          </li>
        </ul>
        <Link to="/app" className="h-button">
          Start free
        </Link>
      </section>
    </SiteLayout>
  )
}
