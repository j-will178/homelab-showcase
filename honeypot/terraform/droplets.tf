# Registered so DigitalOcean doesn't email a root password; root login is disabled on both anyway.
resource "digitalocean_ssh_key" "admin" {
  name       = "hp-admin"
  public_key = var.admin_ssh_public_key
}

# ---- Bastion: long-lived. Patches itself (unattended-upgrades). Holds no secrets. ----
resource "digitalocean_droplet" "bastion" {
  name          = "hp-bastion"
  image         = var.bastion_image
  region        = var.region
  size          = var.bastion_size
  vpc_uuid      = digitalocean_vpc.bastion.id
  tags          = [digitalocean_tag.bastion.id]
  ssh_keys      = [digitalocean_ssh_key.admin.id]
  ipv6          = false
  monitoring    = false
  droplet_agent = false
  user_data = templatefile("${path.module}/bastion.cloud-init.yaml.tftpl", {
    host_key_private = var.bastion_host_key.private
    host_key_public  = var.bastion_host_key.public
    lab_key          = var.lab_ssh_public_key
    admin_key        = var.admin_ssh_public_key
  })
}

# The lab connects to this address; it survives a bastion replacement.
resource "digitalocean_reserved_ip" "bastion" {
  region = var.region
}

resource "digitalocean_reserved_ip_assignment" "bastion" {
  ip_address = digitalocean_reserved_ip.bastion.ip_address
  droplet_id = digitalocean_droplet.bastion.id
}

# ---- Honeypot: disposable, replaced from a fresh Packer image every week. ----
resource "digitalocean_droplet" "honeypot" {
  name          = "hp-honeypot"
  image         = data.digitalocean_image.honeypot.id
  region        = var.region
  size          = var.honeypot_size
  vpc_uuid      = digitalocean_vpc.honeypot.id
  tags          = [digitalocean_tag.honeypot.id]
  ssh_keys      = [digitalocean_ssh_key.admin.id]
  ipv6          = false
  monitoring    = false
  droplet_agent = false
  user_data = templatefile("${path.module}/honeypot.cloud-init.yaml.tftpl", {
    host_key_private = var.honeypot_host_key.private
    host_key_public  = var.honeypot_host_key.public
    lab_key          = var.lab_ssh_public_key
    admin_key        = var.admin_ssh_public_key
    # Outbound traffic leaves a droplet from its own address, not the reserved IP.
    bastion_ip       = digitalocean_droplet.bastion.ipv4_address
    mode             = var.mode
  })

  lifecycle {
    create_before_destroy = true
  }
}

data "digitalocean_image" "honeypot" {
  name = var.honeypot_image
}
