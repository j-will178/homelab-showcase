output "exposure" {
  description = "lab-only (Cowrie reachable from home only) or public."
  value       = var.exposure
}

output "bastion_address" {
  description = "Where the lab connects (reserved IP)."
  value       = digitalocean_reserved_ip.bastion.ip_address
}

output "honeypot_address" {
  description = "The honeypot's public address (what attackers hit)."
  value       = digitalocean_droplet.honeypot.ipv4_address
}

output "ssh_config" {
  description = "For ~/.ssh/config on cloud-ops (the puller uses these host names)."
  value       = <<-EOT
    Host hp-bastion
        HostName ${digitalocean_reserved_ip.bastion.ip_address}
        Port 2222
        User jump
        HostKeyAlias hp-bastion
    Host hp-honeypot
        HostName ${digitalocean_droplet.honeypot.ipv4_address}
        Port 47022
        User collector
        HostKeyAlias hp-honeypot
        ProxyJump hp-bastion
  EOT
}

output "known_hosts" {
  description = "Pinned host keys for the two aliases above."
  value       = "hp-bastion ${var.bastion_host_key.public}\nhp-honeypot ${var.honeypot_host_key.public}\n"
  sensitive   = true
}
