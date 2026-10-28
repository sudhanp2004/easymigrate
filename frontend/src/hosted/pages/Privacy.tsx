import { SiteLayout } from '../SiteLayout'

export function Privacy() {
  return (
    <SiteLayout>
      <article className="h-doc">
        <h1>Privacy</h1>
        <p className="h-lead">Short version: we keep as little as possible, encrypted, for at most 7 days.</p>

        <h2>What EasyMigrate stores</h2>
        <ul>
          <li>
            The results of your scan and the choices you make: the configuration of the AWS resources
            in the region you scanned (names, sizes, network settings, tags), and the generated report
            and Terraform.
          </li>
          <li>Your AWS account number, the region, and the name (ARN) of the role you created.</li>
          <li>A random secret code for the role (the external ID) and a session cookie.</li>
        </ul>
        <p>
          We never receive or store AWS passwords or access keys. The role gives EasyMigrate
          temporary, read-only credentials for each scan; they are used in memory and never saved.
          We never read your data: no database contents, disk contents or bucket files.
        </p>

        <h2>How it's protected</h2>
        <p>
          Scan results are encrypted before they are stored. Only your browser's session cookie can
          open your session. The connection is always HTTPS.
        </p>

        <h2>How long</h2>
        <p>
          Everything is deleted automatically 7 days after your last visit. Click{' '}
          <strong>Delete my data</strong> in the app to delete it immediately.
        </p>

        <h2>Removing EasyMigrate's access to AWS</h2>
        <p>
          In the AWS console, open CloudFormation, select the <code>EasyMigrateReadOnly-…</code> stack
          and click Delete. This removes the role; EasyMigrate can no longer read anything.
        </p>

        <h2>Cookies and tracking</h2>
        <p>One cookie keeps your session. No analytics, advertising or third-party trackers.</p>
      </article>
    </SiteLayout>
  )
}
