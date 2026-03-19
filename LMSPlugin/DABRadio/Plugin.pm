package Plugins::DABRadio::Plugin;

use strict;

use vars qw($VERSION);
use base qw(Slim::Plugin::OPMLBased);

use Plugins::DABRadio::Settings;

use Slim::Utils::Log;
use Slim::Utils::Prefs;
use Slim::Utils::Strings qw(string);

$VERSION = '1.0';

my $log = Slim::Utils::Log->addLogCategory({
    'category'     => 'plugin.dabradio',
    'defaultLevel' => 'WARN',
    'description'  => 'DAB Radio Plugin',
});

my $prefs = preferences('plugin.dabradio');

$prefs->init({
    daemon_url   => 'http://localhost:9980',
    icecast_host => '',
    icecast_port => 8000,
});

sub initPlugin {
    my $class = shift;

    Plugins::DABRadio::Settings->new();

    $class->SUPER::initPlugin(
        feed   => \&handleFeed,
        tag    => 'dabradio',
        menu   => 'radios',
        weight => 10,
    );
}

sub getDisplayName {
    return 'PLUGIN_DABRADIO_MODULE_NAME';
}

sub handleFeed {
    my ($client, $cb, $args) = @_;

    my $daemon_url   = $prefs->get('daemon_url')   || '';
    my $icecast_host = $prefs->get('icecast_host') || '';
    my $icecast_port = $prefs->get('icecast_port') || 8000;

    Slim::Networking::SimpleAsyncHTTP->new(
        sub {
            my $http  = shift;
            my $muxes = eval { JSON::XS::decode_json($http->content) };

            if ($@ || !$muxes || !@$muxes) {
                $cb->({ items => [{ type => 'text', name => string('PLUGIN_DABRADIO_NO_SERVICES') }] });
                return;
            }

            my @items;
            for my $mux (@$muxes) {
                for my $svc (@{ $mux->{services} || [] }) {
                    my $url = $svc->{stream};
                    if ($icecast_host) {
                        (my $mount = $url) =~ s|^https?://[^/]+||;
                        $url = "http://$icecast_host:$icecast_port$mount";
                    }
                    push @items, {
                        type => 'audio',
                        name => $svc->{name},
                        url  => $url,
                        icon => 'plugins/DABRadio/html/images/LyrionDABRadio_svg.png',
                    };
                }
            }

            @items = sort { lc($a->{name}) cmp lc($b->{name}) } @items;

            $cb->({ items => \@items });
        },
        sub {
            $log->warn('DAB daemon fetch failed: ' . $_[1]);
            $cb->({ items => [{ type => 'text', name => 'Could not reach DAB daemon at: ' . $daemon_url }] });
        },
        { timeout => 5 },
    )->get("$daemon_url/muxes");
}

1;
