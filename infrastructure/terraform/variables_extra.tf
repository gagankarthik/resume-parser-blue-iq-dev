# GitHub OIDC — for keyless CI/CD authentication
variable "github_owner" {
  description = "GitHub username (personal account) or organisation name that owns the repository"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without owner prefix)"
  type        = string
  default     = "resume-parser-blue-iq-dev"
}
