# Your EasyMigrate download

This folder holds the Google Cloud version of the AWS setup EasyMigrate scanned.

- `report.md`: everything EasyMigrate found, every difference between AWS and Google Cloud,
  and the choice made for each. Read this first.
- `terraform/`: the Google Cloud infrastructure as Terraform code, ready to apply.

Nothing has been created in Google Cloud yet, and nothing was changed in AWS. EasyMigrate
copies configuration only: no database rows, disk contents or bucket files.

## Apply it to Google Cloud

You need a Google Cloud project with billing turned on, and these two tools:

1. **OpenTofu** (or Terraform 1.6+): https://opentofu.org/docs/intro/install/
2. **Google Cloud CLI**: https://cloud.google.com/sdk/docs/install

Then, in a terminal:

```sh
# 1. Sign in so OpenTofu can create resources in your project
gcloud auth application-default login

# 2. Check the project and region (edit terraform/terraform.tfvars to change them)
#    project = "{gcp_project}"
#    region  = "{gcp_region}"

# 3. Database passwords (AWS never reveals the old ones, so choose new ones)
{password_vars}

# 4. Create everything
cd terraform
tofu init
tofu plan      # shows exactly what will be created
tofu apply     # asks for "yes" before creating anything
```

If `tofu plan` says an API is not enabled, run the `gcloud services enable ...` command it
prints and try again.

## Good to know

- Terraform keeps track of what it created in `terraform/terraform.tfstate`. Keep that file
  safe; it is how `tofu destroy` knows what to delete. To share it with a team, move it to
  a Cloud Storage bucket: add a `backend "gcs"` block (see the OpenTofu docs) and run
  `tofu init -migrate-state`.
- To delete everything this created: `tofu destroy` in the `terraform` folder.
- Prefer EasyMigrate to plan, apply and undo for you? Use the command-line version:
  https://github.com/sudhanp2004/easymigrate
