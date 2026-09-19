provider "aws" {
  region = "ap-south-1"
}

variable "db_password" {
  description = "Master password for the test RDS instance (set via TF_VAR_db_password)"
  type        = string
  sensitive   = true
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

  depends_on = [aws_iam_role_policy_attachment.eks_cluster_policy]
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
