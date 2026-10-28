import { Link } from 'react-router-dom'
import { SiteLayout } from '../SiteLayout'

export function Guide() {
  return (
    <SiteLayout>
      <article className="h-doc">
        <h1>How to use EasyMigrate</h1>
        <p className="h-lead">
          From your AWS account to a ready-to-apply Google Cloud setup, step by step. No cloud
          expertise needed; every term is explained the first time it appears.
        </p>
        <nav className="h-toc">
          <a href="#what">1. What it does</a>
          <a href="#connect">2. Connect AWS</a>
          <a href="#scan">3. Scan</a>
          <a href="#decisions">4. Differences and decisions</a>
          <a href="#download">5. Download and apply</a>
          <a href="#faq">6. Questions</a>
        </nav>

        <h2 id="what">1. What EasyMigrate does (and doesn't)</h2>
        <p>
          EasyMigrate looks at the infrastructure you run on AWS (networks, servers, databases,
          Kubernetes, storage buckets, load balancers) and works out the closest equivalent on Google
          Cloud. Where the two clouds behave differently it tells you, picks a safe option and lets
          you change it. At the end you download <strong>Terraform</strong> code: a standard,
          widely-used format that describes cloud resources and creates them when you run it.
        </p>
        <ul>
          <li>It <strong>reads</strong> AWS. It never changes, creates or deletes anything there.</li>
          <li>It copies <strong>configuration</strong>, not data: no database rows, disk contents or files.</li>
          <li>It doesn't create anything in Google Cloud. You run the downloaded code yourself.</li>
        </ul>

        <h2 id="connect">2. Connect your AWS account</h2>
        <p>
          Instead of asking for passwords or access keys, EasyMigrate asks you to create a{' '}
          <strong>role</strong> in your AWS account. A role is a set of permissions that a trusted
          outside service can borrow for a short time. This one only allows reading, and only
          EasyMigrate can borrow it, using a secret code made for your browser session (AWS calls it
          an <em>external ID</em>).
        </p>
        <ol>
          <li>Pick the AWS region where your resources run (for example ap-south-1 is Mumbai).</li>
          <li>
            Click <strong>Open AWS and create the role</strong>. AWS opens <em>CloudFormation</em>, its
            tool for creating resources from a template, with everything filled in. Sign in if asked.
          </li>
          <li>
            At the bottom, tick <em>“I acknowledge that AWS CloudFormation might create IAM resources
            with custom names”</em> (IAM is AWS's permissions system) and click{' '}
            <strong>Create stack</strong>.
          </li>
          <li>
            Wait about a minute until the status says <strong>CREATE_COMPLETE</strong>. Open the{' '}
            <strong>Outputs</strong> tab and copy <strong>RoleArn</strong>. An ARN is AWS's full name
            for something; it looks like <code>arn:aws:iam::123456789012:role/EasyMigrateReadOnly-1a2b3c4d</code>.
          </li>
          <li>Paste it into EasyMigrate and click Connect. You'll see your AWS account number.</li>
        </ol>
        <p className="h-hint">
          Got “AWS refused access to that role”? Check the stack finished creating, and that you used
          the link from this same browser session. A stack created from an older session has a
          different external ID: delete it and create a new one from the current link.
        </p>
        <p>
          <strong>Removing access:</strong> in the AWS console open CloudFormation, select the{' '}
          <code>EasyMigrateReadOnly-…</code> stack and click Delete. That removes the role.
        </p>

        <h2 id="scan">3. Scan</h2>
        <p>
          Click <strong>Start scan</strong>. EasyMigrate reads the configuration of the region you
          chose. A small account takes a minute or two. If a permission is missing for something,
          the scan still finishes and the report lists it as “could not read”.
        </p>

        <h2 id="decisions">4. Differences and decisions</h2>
        <p>
          AWS and Google Cloud are similar but not identical. For example, an AWS subnet lives in one
          data centre (a <em>zone</em>) while a Google Cloud subnet covers a whole region. Each
          difference like this is listed with:
        </p>
        <ul>
          <li>what is different and why it matters,</li>
          <li>the options for handling it, each with its effect and risk,</li>
          <li>a <strong>safe default</strong>, already selected.</li>
        </ul>
        <p>
          Most people keep the defaults. A few differences need a human (for example, something
          Google Cloud has no equivalent for); those are marked <em>manual</em> and need a tick to
          confirm you've read them. You can't download until every manual one is ticked.
        </p>

        <h2 id="download">5. Download and apply</h2>
        <p>
          Optionally enter your Google Cloud project ID (you can also fill it in later) and check
          the region, then download the zip. Inside:
        </p>
        <ul>
          <li><code>report.md</code>: everything found, every difference and the choice made. Read it first.</li>
          <li><code>terraform/</code>: the Google Cloud setup as code.</li>
          <li><code>README.md</code>: the exact commands to run.</li>
        </ul>
        <p>To create it in Google Cloud you need a project with billing on, then:</p>
        <pre className="h-code">{`gcloud auth application-default login
cd terraform
tofu init
tofu plan     # shows what will be created
tofu apply    # creates it after you type "yes"`}</pre>
        <p className="h-hint">
          <code>tofu</code> is OpenTofu, the open-source Terraform (the <code>terraform</code> command
          works the same). <code>gcloud</code> is Google's command-line tool. Links to install both are
          in the README.
        </p>

        <h2 id="faq">6. Questions</h2>
        <h3>Is it safe to connect my AWS account?</h3>
        <p>
          The role can only read configuration, only EasyMigrate can use it, and only with your
          session's secret code. You can read the template before creating it and delete it any time.
        </p>
        <h3>What do you store, and for how long?</h3>
        <p>
          Your scan results and choices, encrypted, plus the role name. Never AWS keys or passwords.
          Everything is deleted 7 days after your last visit, or immediately when you click “Delete
          my data”. See <Link to="/privacy">Privacy</Link>.
        </p>
        <h3>What does it cost?</h3>
        <p>
          EasyMigrate is free. Running your resources in Google Cloud costs what Google charges, as it
          would however you created them. Scanning AWS uses only free read calls.
        </p>
        <h3>Can EasyMigrate create the Google Cloud resources for me?</h3>
        <p>
          Not on this website yet. The{' '}
          <a href="https://github.com/sudhanp2004/easymigrate" target="_blank" rel="noopener noreferrer">
            command-line version
          </a>{' '}
          can plan, apply and undo for you.
        </p>
        <h3>What's not covered?</h3>
        <p>
          Data (database contents, disks, bucket files), and AWS services beyond those on the{' '}
          <Link to="/">home page</Link>. Anything EasyMigrate skips is listed in the report.
        </p>

        <Link to="/app" className="h-button">
          Start free
        </Link>
      </article>
    </SiteLayout>
  )
}
