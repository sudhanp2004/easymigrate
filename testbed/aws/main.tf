provider "aws" {
  region = "ap-south-1"
}

variable "db_password" {
  description = "Master password for the test RDS instance (set via TF_VAR_db_password)"
  type        = string
  sensitive   = true
}

variable "admin_cidr" {
  description = "Your public IP as a /32; the app accepts HTTP and SSH from it only (TF_VAR_admin_cidr)"
  type        = string
}

variable "ssh_public_key" {
  description = "OpenSSH public key for the test key pair; VM-SSH-KEYS imports it (TF_VAR_ssh_public_key)"
  type        = string
}

# 1. Create a basic VPC
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  tags = {
    Name = "migration-test-vpc"
  }
}

# 2. Create an Internet Gateway
resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "migration-test-igw"
  }
}

# 3. Create a Route Table pointing traffic to the Internet Gateway
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }

  tags = {
    Name = "migration-test-rt"
  }
}

# 4. Create two public subnets in different Availability Zones
resource "aws_subnet" "public_1" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "ap-south-1a"
  map_public_ip_on_launch = true
  tags = {
    Name = "migration-test-subnet-1"
  }
}

resource "aws_subnet" "public_2" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "ap-south-1b"
  map_public_ip_on_launch = true
  tags = {
    Name = "migration-test-subnet-2"
  }
}

# 5. Link both subnets to the public Route Table
resource "aws_route_table_association" "sub1" {
  subnet_id      = aws_subnet.public_1.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "sub2" {
  subnet_id      = aws_subnet.public_2.id
  route_table_id = aws_route_table.public.id
}

# 5a. A private subnet with a NAT gateway (plan 4 B5)
resource "aws_subnet" "private_1" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.10.0/24"
  availability_zone = "ap-south-1a"
  tags = {
    Name = "migration-test-subnet-private-1"
  }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags = {
    Name = "migration-test-nat-eip"
  }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public_1.id
  tags = {
    Name = "migration-test-nat"
  }
  depends_on = [aws_internet_gateway.gw]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = {
    Name = "migration-test-rt-private"
  }
}

resource "aws_route_table_association" "private_1" {
  subnet_id      = aws_subnet.private_1.id
  route_table_id = aws_route_table.private.id
}

# 5b. A custom NACL on the private subnet (manual-only difference in EasyMigrate)
resource "aws_network_acl" "private" {
  vpc_id     = aws_vpc.main.id
  subnet_ids = [aws_subnet.private_1.id]

  ingress {
    rule_no    = 100
    protocol   = "-1"
    action     = "allow"
    cidr_block = "10.0.0.0/16"
    from_port  = 0
    to_port    = 0
  }

  egress {
    rule_no    = 100
    protocol   = "-1"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 0
    to_port    = 0
  }

  tags = {
    Name = "migration-test-nacl-private"
  }
}

# 5c. Two security groups that reference each other (PolicyPeer both directions)
resource "aws_security_group" "app" {
  name        = "migration-test-app-sg"
  description = "App tier"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "migration-test-app-sg"
  }
}

resource "aws_security_group" "db" {
  name        = "migration-test-db-sg"
  description = "DB tier"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "migration-test-db-sg"
  }
}

resource "aws_security_group_rule" "app_to_db" {
  type                     = "egress"
  security_group_id        = aws_security_group.app.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.db.id
}

resource "aws_security_group_rule" "db_from_app" {
  type                     = "ingress"
  security_group_id        = aws_security_group.db.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.app.id
}

# 6. Create a DB subnet group spanning both AZs
resource "aws_db_subnet_group" "default" {
  name       = "migration-db-subnet-group"
  subnet_ids = [aws_subnet.public_1.id, aws_subnet.public_2.id]

  tags = {
    Name = "migration-db-subnet-group"
  }
}

# 7. Create a small RDS PostgreSQL instance (using major version 15 to avoid mismatch)
resource "aws_db_instance" "postgres" {
  identifier           = "test-postgres-db"
  engine               = "postgres"
  engine_version       = "15"
  instance_class       = "db.t3.micro"
  allocated_storage    = 20
  db_name              = "testdb"
  username             = "dbadmin"
  password             = var.db_password
  skip_final_snapshot  = true
  publicly_accessible  = true
  db_subnet_group_name = aws_db_subnet_group.default.name
}

# 7b. A private MySQL instance, so a live run exercises the Cloud SQL private-IP path
# (Private Services Access) alongside the public Postgres one above, and DB-PARAM-FLAGS
# has a real parameter group to translate. Single-AZ deliberately: Multi-AZ doubles the
# hourly cost and DB-MULTI-AZ is covered by unit tests.
resource "aws_security_group" "mysql" {
  name        = "migration-test-mysql-sg"
  description = "Private MySQL tier"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "migration-test-mysql-sg"
  }
}

resource "aws_security_group_rule" "vpc_to_mysql" {
  type              = "ingress"
  security_group_id = aws_security_group.mysql.id
  from_port         = 3306
  to_port           = 3306
  protocol          = "tcp"
  cidr_blocks       = [aws_vpc.main.cidr_block]
}

resource "aws_db_parameter_group" "mysql" {
  name   = "migration-test-mysql-params"
  family = "mysql8.0"

  # One flag Cloud SQL supports, and one it does not, so DB-PARAM-FLAGS is exercised
  # in both directions on a real scan.
  parameter {
    name  = "max_connections"
    value = "150"
  }

  parameter {
    name  = "max_heap_table_size"
    value = "33554432"
  }
}

resource "aws_db_instance" "mysql" {
  identifier              = "test-mysql-db"
  engine                  = "mysql"
  engine_version          = "8.0"
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  db_name                 = "testdb"
  username                = "dbadmin"
  password                = var.db_password
  skip_final_snapshot     = true
  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 1
  db_subnet_group_name    = aws_db_subnet_group.default.name
  parameter_group_name    = aws_db_parameter_group.mysql.name
  vpc_security_group_ids  = [aws_security_group.mysql.id]
}

# 8. IAM Role for EKS Cluster
resource "aws_iam_role" "eks_cluster" {
  name = "migration-test-eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster.name
}

# 9. EKS Cluster
resource "aws_eks_cluster" "main" {
  name     = "test-eks-cluster"
  role_arn = aws_iam_role.eks_cluster.arn

  vpc_config {
    subnet_ids = [aws_subnet.public_1.id, aws_subnet.public_2.id]
  }

  # K8S-LOGGING: api maps to APISERVER; audit becomes Cloud Audit Logs.
  enabled_cluster_log_types = ["api", "audit"]

  depends_on = [aws_iam_role_policy_attachment.eks_cluster_policy]
}

# K8S-ADDONS: a managed add-on GKE has built in.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "vpc-cni"
}

# 10. IAM Role for EKS Worker Nodes
resource "aws_iam_role" "eks_nodes" {
  name = "migration-test-eks-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_amazon_eks_worker_node_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.eks_nodes.name
}

resource "aws_iam_role_policy_attachment" "node_amazon_eks_cni_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.eks_nodes.name
}

resource "aws_iam_role_policy_attachment" "node_amazon_ec2_container_registry_read_only" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.eks_nodes.name
}

# 11. EKS Node Group (1 micro node for testing)
resource "aws_eks_node_group" "nodes" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "test-node-group"
  node_role_arn   = aws_iam_role.eks_nodes.arn
  subnet_ids      = [aws_subnet.public_1.id, aws_subnet.public_2.id]
  instance_types  = ["t3.micro"]

  scaling_config {
    desired_size = 1
    max_size     = 2
    min_size     = 1
  }

  # Carried exactly to the GKE node pool (spec 5 §4.2).
  labels = {
    role = "general"
  }

  taint {
    key    = "dedicated"
    value  = "test"
    effect = "PREFER_NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_amazon_eks_worker_node_policy,
    aws_iam_role_policy_attachment.node_amazon_eks_cni_policy,
    aws_iam_role_policy_attachment.node_amazon_ec2_container_registry_read_only,
  ]
}
# 12. A standalone EC2 instance, for the compute family (spec 4 live run).
#
# Deliberately attached to BOTH the app security group and the MySQL one. The MySQL group
# guards an RDS instance too, so this is the shared-policy case spec 4 §2.1 describes: before
# the plan-2 fix, DB-SG-FIREWALL read such a group as RDS-only and would have dropped it,
# stripping the firewall rules from this instance.
#
# The app group also has an egress rule targeting the db group, which exercises the
# NET-EGRESS-TAG narrowing (spec 4 §10): with an instance behind the peer, the destination
# should resolve to that instance's subnet rather than the whole VPC.
resource "aws_instance" "app" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = "t3.micro"
  subnet_id                   = aws_subnet.public_1.id
  vpc_security_group_ids      = [aws_security_group.app.id, aws_security_group.mysql.id]
  associate_public_ip_address = true
  key_name                    = aws_key_pair.test.key_name

  # VM-USER-DATA: becomes a GCP startup script. Serves a page so the tag seam can be proven
  # by reaching port 80 through the migrated firewall rules.
  # python3 ships with both AL2023 and the GCP stock image, so this needs no internet access —
  # a migrated VM in a public subnet has no NAT, because GCP gives it no external route by default.
  user_data = <<-EOT
    #!/bin/bash
    mkdir -p /srv/www
    echo "easymigrate app on $(hostname)" > /srv/www/index.html
    cd /srv/www && nohup python3 -m http.server 80 > /var/log/http.log 2>&1 &
  EOT

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
  }

  tags = {
    Name = "migration-test-app"
  }
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

# A second, non-boot volume: VM-DISK-EMPTY should recreate it empty at high risk.
resource "aws_ebs_volume" "app_data" {
  availability_zone = aws_subnet.public_1.availability_zone
  size              = 20
  type              = "gp3"

  tags = {
    Name = "migration-test-app-data"
  }
}

resource "aws_volume_attachment" "app_data" {
  device_name = "/dev/xvdb"
  volume_id   = aws_ebs_volume.app_data.id
  instance_id = aws_instance.app.id
}

# An Elastic IP, so VM-PUBLIC-IP has a reserved address to decide about.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = "migration-test-app-eip"
  }

  depends_on = [aws_internet_gateway.gw]
}

# The tag-seam proof (spec 4 §9): the migrated firewall rules carry these to GCP, targeting the
# app group's network tag, so reaching the migrated VM on 80 and 22 proves the tag matches.
resource "aws_security_group_rule" "app_http_from_admin" {
  type              = "ingress"
  security_group_id = aws_security_group.app.id
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = [var.admin_cidr]
}

resource "aws_security_group_rule" "app_ssh_from_admin" {
  type              = "ingress"
  security_group_id = aws_security_group.app.id
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = [var.admin_cidr]
}

# VM-SSH-KEYS: AWS still holds this public key, so import_public_key is the default.
resource "aws_key_pair" "test" {
  key_name   = "migration-test-key"
  public_key = var.ssh_public_key
}

# VM-STOPPED: must arrive in GCP stopped. It sits in the db group on the private subnet, so
# NET-EGRESS-TAG's app->db egress rule narrows to private_1's CIDR instead of the whole VPC
# (spec 4 §10) — the branch the 2026-09-19 live run could not reach.
resource "aws_instance" "stopped" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.private_1.id
  vpc_security_group_ids = [aws_security_group.db.id]

  tags = {
    Name = "migration-test-stopped"
  }
}

resource "aws_ec2_instance_state" "stopped" {
  instance_id = aws_instance.stopped.id
  state       = "stopped"
}

# Spec 4 plan 5: a launch template and a two-AZ ASG with CPU target tracking become an
# instance template, a regional MIG and a regional autoscaler.
resource "aws_launch_template" "web" {
  name          = "migration-test-web-lt"
  image_id      = data.aws_ami.al2023.id
  instance_type = "t3.micro"
  key_name      = aws_key_pair.test.key_name

  vpc_security_group_ids = [aws_security_group.app.id]

  metadata_options {
    http_tokens = "required"
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo "web from asg" > /tmp/hello
  EOT
  )
}

resource "aws_autoscaling_group" "web" {
  name                = "migration-test-web-asg"
  min_size            = 1
  desired_capacity    = 1
  max_size            = 2
  vpc_zone_identifier = [aws_subnet.public_1.id, aws_subnet.public_2.id]

  launch_template {
    id      = aws_launch_template.web.id
    version = aws_launch_template.web.latest_version
  }

  tag {
    key                 = "Name"
    value               = "migration-test-web-asg"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "web_cpu" {
  name                   = "migration-test-web-cpu"
  autoscaling_group_name = aws_autoscaling_group.web.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 50
  }
}

# --- Spec 6: S3 buckets (configuration only; they stay empty) -------------------------------
data "aws_caller_identity" "current" {}

locals {
  bucket_suffix = data.aws_caller_identity.current.account_id
}

# Public website bucket: S3-PUBLIC-ACCESS (defaults private), S3-WEBSITE, S3-CORS, S3-BUCKET-POLICY,
# S3-ACCESS-LOGGING to the logs bucket below.
resource "aws_s3_bucket" "site" {
  bucket = "easymigrate-test-site-${local.bucket_suffix}"
  tags = {
    team = "web"
  }
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.site]
}

resource "aws_s3_bucket_website_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  index_document {
    suffix = "index.html"
  }
  error_document {
    key = "404.html"
  }
}

resource "aws_s3_bucket_cors_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  cors_rule {
    allowed_origins = ["https://example.com"]
    allowed_methods = ["GET", "HEAD"]
    allowed_headers = ["Authorization"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket" "logs" {
  bucket = "easymigrate-test-logs-${local.bucket_suffix}"
}

resource "aws_s3_bucket_logging" "site" {
  bucket        = aws_s3_bucket.site.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "access/"
}

# Object Lock bucket: S3-OBJECT-LOCK (retention policy replaces versioning), S3-LIFECYCLE,
# S3-INTELLIGENT-TIERING (defaults standard because lifecycle moves classes).
resource "aws_s3_bucket" "records" {
  bucket              = "easymigrate-test-records-${local.bucket_suffix}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "records" {
  bucket = aws_s3_bucket.records.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "records" {
  bucket = aws_s3_bucket.records.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.records]
}

resource "aws_s3_bucket_lifecycle_configuration" "records" {
  bucket = aws_s3_bucket.records.id
  rule {
    id     = "archive"
    status = "Enabled"
    filter {
      prefix = "logs/"
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration {
      days = 730
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  depends_on = [aws_s3_bucket_versioning.records]
}

resource "aws_s3_bucket_intelligent_tiering_configuration" "records" {
  bucket = aws_s3_bucket.records.id
  name   = "all"
  tiering {
    access_tier = "ARCHIVE_ACCESS"
    days        = 90
  }
}

# --- Spec 7: load balancers in front of the app instance ------------------------------------
# The ALB admits only the admin IP, so LB-SOURCE-RESTRICTION becomes a Cloud Armor allow-list.
resource "aws_security_group" "alb" {
  name        = "migration-test-alb-sg"
  description = "ALB, admin only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "app_http_from_alb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.app.id
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "web" {
  name               = "migration-test-alb"
  load_balancer_type = "application"
  subnets            = [aws_subnet.public_1.id, aws_subnet.public_2.id]
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "web" {
  name     = "migration-test-web-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id
  health_check {
    path = "/"
  }
}

resource "aws_lb_target_group_attachment" "web" {
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = aws_instance.app.id
}

resource "aws_lb_listener" "web" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.web.arn
  priority     = 10
  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# NLB passthrough on SSH: logging in through it proves the passthrough path end to end.
resource "aws_lb" "ssh" {
  name               = "migration-test-nlb"
  load_balancer_type = "network"
  subnets            = [aws_subnet.public_1.id, aws_subnet.public_2.id]
}

resource "aws_lb_target_group" "ssh" {
  name     = "migration-test-ssh-tg"
  port     = 22
  protocol = "TCP"
  vpc_id   = aws_vpc.main.id
}

resource "aws_lb_target_group_attachment" "ssh" {
  target_group_arn = aws_lb_target_group.ssh.arn
  target_id        = aws_instance.app.id
}

resource "aws_lb_listener" "ssh" {
  load_balancer_arn = aws_lb.ssh.arn
  port              = 22
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ssh.arn
  }
}
