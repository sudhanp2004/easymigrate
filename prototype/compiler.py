import json

def compile_to_gcp():
    with open("migration_ir.json", "r") as f:
        ir = json.load(f)

    print("Compiling IR (VPCs, Subnets, RDS, and GKE) to GCP Terraform...")

    region_map = {"ap-south-1": "asia-south1"}
    target_region = region_map.get(ir["region"], "us-central1")

    tf_code = f'''provider "google" {{
  project = "your-gcp-project-id"
  region  = "{target_region}"
}}

'''

    # Network
    for vpc in ir["network"]["vpcs"]:
        tf_code += f'''resource "google_compute_network" "vpc" {{
  name                    = "{vpc['name']}"
  auto_create_subnetworks = false
}}

'''

    for subnet in ir["network"]["subnets"]:
        tf_code += f'''resource "google_compute_subnetwork" "{subnet['name'].replace('-', '_')}" {{
  name          = "{subnet['name']}"
  ip_cidr_range = "{subnet['cidr']}"
  region        = "{target_region}"
  network       = google_compute_network.vpc.id
}}

'''

    # Databases (RDS -> Cloud SQL)
    for db in ir["databases"]:
        major_version = db["version"].split('.')[0]
        tf_code += f'''resource "google_sql_database_instance" "postgres" {{
  name             = "{db['identifier']}"
  database_version = "POSTGRES_{major_version}"
  region           = "{target_region}"
  settings {{
    tier = "db-f1-micro"
  }}
  deletion_protection = false
}}

resource "google_sql_database" "db" {{
  name     = "{db['db_name']}"
  instance = google_sql_database_instance.postgres.name
}}

'''

    # Kubernetes (EKS -> GKE)
    for cluster in ir.get("kubernetes_clusters", []):
        tf_code += f'''resource "google_container_cluster" "primary" {{
  name                     = "{cluster['name']}"
  location                 = "{target_region}"
  remove_default_node_pool = true
  initial_node_count       = 1
  network                  = google_compute_network.vpc.id
  subnetwork               = google_compute_subnetwork.migration_test_subnet_1.id
  deletion_protection      = false
}}

resource "google_container_node_pool" "node_pool" {{
  name     = "migration-gke-node-pool"
  location = "{target_region}"
  cluster  = google_container_cluster.primary.name
  node_count = 1

  node_config {{
    machine_type = "e2-medium"
    oauth_scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
    ]
  }}
}}
'''

    with open("gcp_output/gcp_main.tf", "w") as f:
        f.write(tf_code)

    print("Compilation complete! Full stack GCP code generated in gcp_output/gcp_main.tf")

if __name__ == "__main__":
    compile_to_gcp()