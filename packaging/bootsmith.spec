# RPM for Bootsmith.
#
# Ships the systemd unit and its host settings -- NOT the application, which
# runs from the container image on GHCR. The unit is pinned to the image built
# from the same commit, so `rpm -q bootsmith` names exactly what runs and
# `dnf downgrade` is a real rollback. The presets come from the
# bootsmith-config package, built by that repo's own pipeline.
#
# THE VERSION LIVES HERE. Bump specver; the image tag follows it.
%global specver 0.2.0

# $GIT_HASH first: build_rpm.sh computes it on the host and passes it into the
# build container. git alone can resolve to "nogit" there, and the unit would
# then pin an image tag that was never pushed.
%define git_hash %(if [ -n "$GIT_HASH" ]; then echo "$GIT_HASH"; else git rev-parse --short HEAD 2>/dev/null || echo nogit; fi)
%global appimage ghcr.io/gemini-rtsw/bootsmith

Name:           bootsmith
Version:        %{specver}
Release:        1.git%{git_hash}%{?dist}
Summary:        Bootsmith: VME crate boot console (runs as a container)

License:        Proprietary
URL:            https://github.com/gemini-rtsw/bootsmith
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros
Requires:       systemd
Recommends:     bootsmith-config
Requires(pre):  shadow-utils

%description
Web console for setting VME crate boot parameters over WTI terminal servers.

This package contains only the systemd unit and its settings. The
application runs from the container image
%{appimage}:%{version}-git%{git_hash}, which the unit pulls on start.

    systemctl enable --now bootsmith
    curl -s localhost:8080/

Presets live in /var/lib/bootsmith/profiles and are installed by the
bootsmith-config package; install a newer build of it to update them.

%prep
%autosetup

%build
# Pin the NVR-unique tag, not the bare version: :<version> is retagged by every
# build of that version, which would make `dnf downgrade` a no-op for the image.
sed -e 's|@IMAGE@|%{appimage}:%{version}-git%{git_hash}|' \
    deploy/bootsmith.service.in > bootsmith.service
if grep -q '@IMAGE@' bootsmith.service; then
    echo "ERROR: image placeholder not substituted" >&2; exit 1
fi

%install
install -Dpm 0644 bootsmith.service        %{buildroot}%{_unitdir}/bootsmith.service
install -Dpm 0644 deploy/bootsmith.sysconfig %{buildroot}%{_sysconfdir}/sysconfig/bootsmith
install -dm 0755 %{buildroot}%{_sharedstatedir}/bootsmith/profiles

%pre
getent group bootsmith >/dev/null || groupadd -r bootsmith
getent passwd bootsmith >/dev/null || \
    useradd -r -g bootsmith -d %{_sharedstatedir}/bootsmith -s /sbin/nologin \
            -c "Bootsmith service" bootsmith
exit 0

%post
%systemd_post bootsmith.service

%preun
%systemd_preun bootsmith.service

%postun
# No restart on upgrade: someone may be mid-way through booting a crate. The
# new image takes effect on the next `systemctl restart bootsmith`.
%systemd_postun bootsmith.service

%files
# Not a config file: the unit carries the image tag, so upgrades must replace it.
%{_unitdir}/bootsmith.service
%config(noreplace) %{_sysconfdir}/sysconfig/bootsmith
%dir %attr(0755,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith
# World-writable so operators can edit without root. The app saves by writing
# a temp file and renaming it, so it can update any preset in here, whoever owns
# the file. The presets themselves belong to bootsmith-config.
%dir %attr(0777,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith/profiles

%changelog
* Wed Oct 14 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.2.0-1
- Stop shipping the presets. They come from the bootsmith-config package,
  built by that repo's pipeline, so updating presets is installing an RPM.

* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-3
- On first install, make the profiles directory a shallow clone of
  bootsmith-config shipped in this package; profiles are no longer
  package-owned files.

* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-2
- Make /var/lib/bootsmith/profiles writable by all users.

* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-1
- Package for the gemini-rtsw-ci pipeline: systemd unit pinned to the image.
