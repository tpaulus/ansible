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

function ansible-macos-hacks
    # https://docs.ansible.com/ansible/latest/reference_appendices/faq.html#running-on-macos-as-a-control-node
    set -x OBJC_DISABLE_INITIALIZE_FORK_SAFETY YES

    # https://github.com/prometheus-community/ansible/issues/165
    set -x no_proxy "*"
end

function ansible-playbook
    ansible-macos-hacks

    set temp_file (mktemp)
    echo "Obtaining Vault Password for 1Password"
    set vault_data (op item get "Ansible" --vault="Infra" --fields type=concealed --format json)
    echo $vault_data | jq -r '.[] | select(.id == "password") | .value'  $vault_password > $temp_file
    
    # Set additional fields as environment variables
    set -l IFS '\n'
    for field in (echo $vault_data | jq -r '.[] | select(.id != "password") | .label + "=" + .value')
        set -x (string split '=' $field)
    end
    
    command ansible-playbook $argv --vault-password-file $temp_file 
    
    # Clean up temp file
    rm $temp_file
    
    # Clean up environment variables
    for field in (echo $vault_data | jq -r '.[] | select(.id != "password") | .label')
        set -e $field
    end
end


function ansible-vault
    ansible-macos-hacks

    set temp_file (mktemp)
    echo "Obtaining Vault Password for 1Password"
    set vault_password (op item get "Ansible" --vault="Infra" --fields=password)
    echo $vault_password > $temp_file
    command ansible-vault $argv --vault-password-file $temp_file
    rm $temp_file
end

function ansible-inventory
    ansible-macos-hacks

    set temp_file (mktemp)
    echo "Obtaining Vault Password for 1Password"
    set vault_data (op item get "Ansible" --vault="Infra" --fields type=concealed --format json)
    echo $vault_data | jq -r '.[] | select(.id == "password") | .value'  $vault_password > $temp_file
    
    # Set additional fields as environment variables
    set -l IFS '\n'
    for field in (echo $vault_data | jq -r '.[] | select(.id != "password") | .label + "=" + .value')
        set -x (string split '=' $field)
    end
    
    command ansible-inventory $argv --vault-password-file $temp_file
    
    # Clean up temp file
    rm $temp_file
    
    # Clean up environment variables
    for field in (echo $vault_data | jq -r '.[] | select(.id != "password") | .label')
        set -e $field
    end
end

```

## Galaxy
Install necessary Galaxy Collections & Roles via...
```shell
ansible-galaxy install -r requirements.yml
```