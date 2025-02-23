Ansible Playbooks
-----------------

Ansible Playbooks for common server configuration and managment.

## Host Dependencies
On a Mac...
```shell
brew install python-pytz unzip gnu-tar
```

If you want to be able to lint files...
```shell
brew install ansible-lint
```

## Fish Wrappers
Local secrets are stored in 1Password, which can be accessed via the 1Password CLI (op).
To make this not super clumsy, a handful of Fish functions wrap the ansible commands to
hid all of this. Additionally, any password fields (that are not called password, as that is
reserved for the Vault Passphrase) are copied as enviroment variables.

```fish

function ansible-playbook
    ansible-macos-hacks

    # Set EnvVars for Ansible
    set -x NETBOX_TOKEN $(op read "op://Infra/Ansible/NETBOX_TOKEN")
    
    command ansible-playbook $argv --vault-id "op://Infra/Ansible/password@vault-1p-client.sh"
    
    # Clean up environment variables
    set -e NETBOX_TOKEN
end


function ansible-vault
    ansible-macos-hacks

    command ansible-vault $argv --vault-id "op://Infra/Ansible/password@vault-1p-client.sh"
end

function ansible-inventory
    ansible-macos-hacks

    # Set EnvVars for Ansible
    set -x NETBOX_TOKEN $(op read "op://Infra/Ansible/NETBOX_TOKEN")
    
    command ansible-inventory $argv --vault-id "op://Infra/Ansible/password@vault-1p-client.sh"
    
    # Clean up environment variables
    set -e NETBOX_TOKEN
end

```

## Galaxy
Install necessary Galaxy Collections & Roles via...
```shell
ansible-galaxy install -r requirements.yml
```