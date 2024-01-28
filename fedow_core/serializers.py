from collections import OrderedDict
from datetime import timedelta
from time import sleep

from django.conf import settings
from django.utils import timezone
from django.utils.timezone import localtime
from rest_framework import serializers
from rest_framework.generics import get_object_or_404

from fedow_core.models import Place, FedowUser, Card, Wallet, Transaction, OrganizationAPIKey, Asset, Token, \
    get_or_create_user, Origin, asset_creator, Configuration, Federation, CheckoutStripe
from fedow_core.utils import get_request_ip, get_public_key, dict_to_b64, verify_signature
from cryptography.hazmat.primitives.asymmetric import rsa
import stripe
import logging

logger = logging.getLogger(__name__)


class HandshakeValidator(serializers.Serializer):
    # Temp fedow place APIkey inside the request header
    fedow_place_uuid = serializers.PrimaryKeyRelatedField(queryset=Place.objects.all())
    cashless_rsa_pub_key = serializers.CharField(max_length=512)
    cashless_ip = serializers.IPAddressField()
    cashless_url = serializers.URLField()
    cashless_admin_apikey = serializers.CharField(max_length=41, min_length=41)
    dokos_id = serializers.CharField(max_length=100, required=False, allow_null=True)

    def validate_fedow_place_uuid(self, value) -> Place:
        # TODO: Si place à déja été configuré, on renvoie un 400
        # if place.cashless_server_ip or place.cashless_server_url or place.cashless_server_key:
        #     logger.error(f"{timezone.localtime()} Place already configured {self.context.get('request').data}")
        #     raise serializers.ValidationError("Place already configured")

        return value

    def validate_cashless_rsa_pub_key(self, value) -> rsa.RSAPublicKey:
        # Valide uniquement le format avec la biblothèque cryptography
        self.pub_key = get_public_key(value)
        if not self.pub_key:
            logger.error(f"{timezone.localtime()} Public rsa key invalid")
            raise serializers.ValidationError("Public rsa key invalid")

        # Public key, but not paired with signature (see validate)
        return value

    def validate_cashless_ip(self, value):
        request = self.context.get('request')
        if value != get_request_ip(request):
            logger.error(f"{timezone.localtime()} ERROR Place create Invalid IP {get_request_ip(request)}")
            raise serializers.ValidationError("Invalid IP")
        return value

    def validate(self, attrs: OrderedDict) -> OrderedDict:
        request = self.context.get('request')
        public_key = self.pub_key
        signed_message = dict_to_b64(request.data)
        signature = request.META.get('HTTP_SIGNATURE')

        if not verify_signature(public_key, signed_message, signature):
            logger.error(f"{timezone.localtime()} ERROR HANDSHAKE Invalid signature - {request.data}")
            raise serializers.ValidationError("Invalid signature")

        # Check if key is the temp given by the manual creation.
        # and if the user associated is admin of the place
        key = request.META["HTTP_AUTHORIZATION"].split()[1]
        api_key = OrganizationAPIKey.objects.get_from_key(key)
        user = api_key.user

        place: Place = attrs.get('fedow_place_uuid')
        if user not in place.admins.all() and place != api_key.place:
            logger.error(f"{timezone.localtime()} ERROR HANDSHAKE user not in place admins - {request.data}")
            raise serializers.ValidationError("Unauthorized")

        if 'temp_' not in api_key.name:
            logger.error(f"{timezone.localtime()} ERROR ApiKey not temp_ : {request.data}")
            raise serializers.ValidationError("Unauthorized")

        return attrs


class OnboardSerializer(serializers.Serializer):
    id_acc_connect = serializers.CharField(max_length=21)
    fedow_place_uuid = serializers.PrimaryKeyRelatedField(queryset=Place.objects.all())

    def validate_id_acc_connect(self, value):
        config = Configuration.get_solo()
        stripe.api_key = config.get_stripe_api()
        self.info_stripe = None
        try:
            info_stripe = stripe.Account.retrieve(value)
            self.info_stripe = info_stripe
        except Exception as exc:
            logger.error(f"Stripe Account.retrieve : {exc}")
            raise serializers.ValidationError("Stripe error")
        if not info_stripe:
            raise serializers.ValidationError("id_acc_connect not a stripe account")
        return value

    def validate_fedow_place_uuid(self, value):
        place: Place = self.context.get('request').place
        if place != value:
            raise serializers.ValidationError("Place not match")
        return value


class PlaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Place
        fields = (
            'uuid',
            'name',
            'dokos_id',
            'wallet',
            'stripe_connect_valid',
        )

    def validate(self, attrs):
        return attrs


class WalletSerializer(serializers.ModelSerializer):
    tokens = serializers.SerializerMethodField()

    def get_tokens(self, obj: Wallet):
        # On ne pousse que les tokens acceptés par le lieu
        if self.context.get('request'):
            request = self.context.get('request')
            place = request.place
            if place:
                assets = place.accepted_assets()
                return TokenSerializer(obj.tokens.filter(wallet=obj, asset__in=assets), many=True).data

        raise serializers.ValidationError("Place not found")

    class Meta:
        model = Wallet
        fields = (
            'uuid',
            'tokens',
        )


class UserSerializer(serializers.ModelSerializer):
    wallet = WalletSerializer(many=False)

    class Meta:
        model = FedowUser
        fields = (
            'uuid',
            'wallet',
        )




class CardRefundOrVoidValidator(serializers.Serializer):
    primary_card_uuid = serializers.PrimaryKeyRelatedField(queryset=Card.objects.all(), required=False)
    primary_card_fisrtTagId = serializers.SlugRelatedField(
        queryset=Card.objects.all(),
        required=False, slug_field='first_tag_id')
    user_card_uuid = serializers.PrimaryKeyRelatedField(queryset=Card.objects.all(), required=False)
    user_card_firstTagId = serializers.SlugRelatedField(
        queryset=Card.objects.all(),
        required=False, slug_field='first_tag_id')
    action = serializers.ChoiceField(choices=Transaction.TYPE_ACTION, required=False, allow_null=True)

    transactions = list()

    def validate_action(self, value):
        if value not in [Transaction.REFUND, Transaction.VOID]:
            raise serializers.ValidationError("Action must be REFUND or VOID")
        return value

    def validate(self, attrs):
        transactions = list()

        request = self.context.get('request')
        self.place: Place = request.place
        # Avons nous une carte user et/ou une carte primaire LaBoutik ?
        self.primary_card = attrs.get('primary_card_uuid') or attrs.get('primary_card_fisrtTagId')
        self.user_card: Card = attrs.get('user_card_uuid') or attrs.get('user_card_firstTagId')

        if self.primary_card not in request.place.primary_cards.all():
            raise serializers.ValidationError("Primary card must be in place primary cards")

        if not self.user_card:
            raise serializers.ValidationError("User card is required for void or refund")

        wallet: Wallet = self.user_card.get_wallet()
        self.ex_wallet_serialized = WalletSerializer(wallet, context=self.context).data

        for token in wallet.tokens.filter(
                value__gt=0,
                asset__wallet_origin=request.place.wallet,
                asset__category__in=[Asset.TOKEN_LOCAL_FIAT, Asset.TOKEN_LOCAL_NOT_FIAT]):
            transaction_dict = {
                "ip": get_request_ip(request),
                "checkout_stripe": None,
                "sender": self.user_card.get_wallet(),
                "receiver": request.place.wallet,
                "asset": token.asset,
                "amount": token.value,
                "action": Transaction.REFUND,
                "primary_card": self.primary_card,
                "card": self.user_card,
                "subscription_start_datetime": None
            }
            transaction = Transaction.objects.create(**transaction_dict)
            transactions.append(TransactionSerializer(transaction, context=self.context).data)

        self.transactions = transactions
        if attrs.get('action') == Transaction.VOID:
            self.user_card.user = None
            self.user_card.wallet_ephemere = None
            self.user_card.save()

        return attrs


class CardCreateValidator(serializers.ModelSerializer):
    generation = serializers.IntegerField(required=True)
    is_primary = serializers.BooleanField(required=True)

    # Lors de la création, si il existe déja des assets dans la carte,
    # on les créé avec l'uuid de l'asset cashless pour une meuilleur correspondance.
    tokens_uuid = serializers.ListField(required=False, allow_null=True)

    def validate_generation(self, value):
        place = self.context.get('request').place
        if not place:
            raise serializers.ValidationError("Place not found")

        if not getattr(self, 'origin', None):
            self.origin, created = Origin.objects.get_or_create(place=place, generation=value)

        if self.origin.generation != value:
            raise serializers.ValidationError("One generation per request")

        return value

    def create(self, validated_data):
        # Le cashless envoie des cartes qui ont déja des tokens.
        # On les créé vide pour faire la correspondance avec l'uuid du cashless.
        pre_tokens = validated_data.pop('tokens_uuid', False)

        is_primary = validated_data.pop('is_primary', False)
        validated_data.pop('generation')
        validated_data['origin'] = self.origin

        card = Card.objects.create(**validated_data)
        if is_primary:
            self.origin.place.primary_cards.add(card)

        if pre_tokens:
            for pre_token in pre_tokens:
                try :
                    asset = Asset.objects.get(uuid=pre_token.get('asset_uuid'))
                except Asset.DoesNotExist:
                    raise serializers.ValidationError("Asset does not exist")

                wallet = card.get_wallet()
                token, created = Token.objects.get_or_create(uuid=pre_token.get("token_uuid"), asset=asset,
                                                             wallet=wallet)
                print(f"token {token} created {created}")
                sleep(0.1)
        return card

    class Meta:
        model = Card
        fields = (
            'uuid',
            'first_tag_id',
            'complete_tag_id_uuid',
            'qrcode_uuid',
            'number_printed',
            'generation',
            'is_primary',
            'tokens_uuid',
        )


class AssetSerializer(serializers.ModelSerializer):
    place_origin = PlaceSerializer(many=False)

    class Meta:
        model = Asset
        fields = (
            'uuid',
            'name',
            'currency_code',
            'category',
            'place_origin',
            'created_at',
            'last_update',
            'is_stripe_primary',
        )

    def to_representation(self, instance: Asset):
        # Add apikey user to representation
        rep = super().to_representation(instance)
        if self.context.get('action') == 'retrieve':
            rep['total_token_value'] = instance.total_token_value()
            rep['total_in_place'] = instance.total_in_place()
            rep['total_in_wallet_not_place'] = instance.total_in_wallet_not_place()
        return rep


class AssetCreateValidator(serializers.Serializer):
    uuid = serializers.UUIDField(required=False)
    name = serializers.CharField()
    currency_code = serializers.CharField(max_length=3)
    category = serializers.ChoiceField(choices=Asset.CATEGORIES)
    created_at = serializers.DateTimeField(required=False)

    def validate_name(self, value):
        if Asset.objects.filter(name=value).exists():
            raise serializers.ValidationError("Asset already exists")
        return value

    def validate_currency_code(self, value):
        #     if Asset.objects.filter(currency_code=value).exists():
        #         raise serializers.ValidationError("Currency code already exists")
        return value.upper()

    def validate(self, attrs):
        request = self.context.get('request')
        place = request.place

        asset_dict = {
            "name": attrs.get('name'),
            "currency_code": attrs.get('currency_code'),
            "category": attrs.get('category'),
            "wallet_origin": place.wallet,
            "ip": get_request_ip(request),
        }

        if attrs.get('uuid'):
            asset_dict["original_uuid"] = attrs.get('uuid')
        if attrs.get('created_at'):
            asset_dict["created_at"] = attrs.get('created_at')

        self.asset = asset_creator(**asset_dict)

        # Pour les tests unitaires :
        # if settings.DEBUG:
        #     federation = Federation.objects.get(name='TEST FED')
        #     federation.assets.add(self.asset)

        if self.asset:
            return attrs
        else:
            raise serializers.ValidationError("Asset creation failed")


class TokenSerializer(serializers.ModelSerializer):
    asset = AssetSerializer(many=False)

    class Meta:
        model = Token
        fields = (
            'uuid',
            'name',
            'value',
            'asset',

            'asset_uuid',
            'asset_name',
            'asset_category',

            'is_primary_stripe_token',
            'last_transaction_datetime',
        )


class OriginSerializer(serializers.ModelSerializer):
    place = PlaceSerializer()

    class Meta:
        model = Origin
        fields = (
            'place',
            'generation',
            'img',
        )


class WalletCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()

    card_first_tag_id = serializers.SlugRelatedField(slug_field='first_tag_id',
                                                     queryset=Card.objects.all(), required=False)
    card_qrcode_uuid = serializers.SlugRelatedField(slug_field='qrcode_uuid',
                                                    queryset=Card.objects.all(), required=False)

    def validate(self, attrs):
        # On trace l'ip de la requete
        ip = None
        request = self.context.get('request')
        if request:
            ip = get_request_ip(request)

        # Récupération de l'email
        self.user = None
        email = attrs.get('email')
        user_exist = FedowUser.objects.filter(email=email).exists()
        if user_exist:
            self.user = FedowUser.objects.get(email=email)

        card: Card = attrs.get('card_first_tag_id') or attrs.get('card_qrcode_uuid')
        self.card = card

        # Si l'email ne donne aucun user, on le créé
        if not card and not user_exist:
            self.user, created = get_or_create_user(email, ip=ip)
            return attrs

        if card and not user_exist:
            # Si une carte et un nouveau mail, liaison si carte vierge:
            if not card.user and not card.wallet_ephemere:
                user, created = get_or_create_user(email, ip=ip)
                self.user = user
                card.user = user
                card.save()
                return attrs
            # Si la carte possède un wallet ephemere, nous créons l'user avec ce wallet.
            elif not card.user and card.wallet_ephemere:
                user, created = get_or_create_user(email, ip=ip, wallet_uuid=card.wallet_ephemere.uuid)
                self.user = user
                card.user = user
                # Le wallet ephemere est devenu un wallet user, on le retire de la carte
                card.wallet_ephemere = None
                card.save()
                return attrs
            elif card.user or card.wallet_ephemere:
                raise serializers.ValidationError("Card already linked to another user")

        # L'utilisateur existe.
        # Si la carte est liée à un user, on vérifie que c'est le même
        if card and user_exist:
            # Si carte vierge, on lie l'user
            if not card.user and not card.wallet_ephemere:
                card.user = self.user
                card.save()
                return attrs
            # La carte n'a pas d'user, mais un wallet ephemere
            if not card.user and card.wallet_ephemere:
                # Si carte avec wallet ephemere, on lie l'user avec le wallet ephemere

                if card.wallet_ephemere != self.user.wallet:
                    # On vide le wallet ephemere en faveur du wallet de l'user
                    for token in card.wallet_ephemere.tokens.filter(value__gt=0):
                        data = {
                            "amount": token.value,
                            "asset": f"{token.asset.pk}",
                            "sender": f"{card.wallet_ephemere.pk}",
                            "receiver": f"{self.user.wallet.pk}",
                            "user_card_uuid": f"{card.pk}",
                            "action": Transaction.FUSION,
                        }
                        transaction_validator = TransactionW2W(data=data, context={'request': self.context['request']})
                        if not transaction_validator.is_valid():
                            logger.error(
                                f"{timezone.localtime()} ERROR FUSION WalletCreateSerializer : {transaction_validator.errors}")
                            raise serializers.ValidationError(transaction_validator.errors)


                # On retire le wallet ephemere de la carte après avoir vérifié qu'il est bien vide
                card.refresh_from_db()
                card.wallet_ephemere.refresh_from_db()
                if card.wallet_ephemere.tokens.filter(value__gt=0).exists():
                    raise serializers.ValidationError("Wallet ephemere not empty after fusion")
                card.wallet_ephemere = None

                card.user = self.user
                card.save()
                return attrs

            if card.user == self.user:
                return attrs
            else:
                raise serializers.ValidationError("Card already linked to another user")

        raise serializers.ValidationError("User not found ?")

    def to_representation(self, instance):
        # Add apikey user to representation
        representation = super().to_representation(instance)
        self.wallet = self.user.wallet
        representation['wallet'] = f"{self.user.wallet.uuid}"
        return representation


class CardSerializer(serializers.ModelSerializer):
    # Un MethodField car le wallet peut être celui de l'user ou celui de la carte anonyme.
    # Faut lancer la fonction get_wallet() pour avoir le bon wallet...
    wallet = serializers.SerializerMethodField()
    origin = OriginSerializer()

    def get_place_origin(self, obj: Card):
        return f"{obj.origin.place.name} V{obj.origin.generation}"

    def get_wallet(self, obj: Card):
        wallet: Wallet = obj.get_wallet()
        return WalletSerializer(wallet, context=self.context).data

    class Meta:
        model = Card
        fields = (
            'first_tag_id',
            'wallet',
            'origin',
            'uuid',
            'qrcode_uuid',
            'number_printed',
            'is_wallet_ephemere',
        )



class BadgeValidator(serializers.Serializer):
    first_tag_id = serializers.CharField(min_length=8, max_length=8)
    primary_card_firstTagId = serializers.CharField(min_length=8, max_length=8)
    asset = serializers.PrimaryKeyRelatedField(queryset=Asset.objects.all())
    pos_uuid = serializers.UUIDField(required=False, allow_null=True)
    pos_name = serializers.CharField(required=False, allow_null=True)

    def validate_first_tag_id(self, first_tag_id):
        self.card = get_object_or_404(Card, first_tag_id=first_tag_id)
        return first_tag_id

    def validate_primary_card_firstTagId(self, primary_card_firstTagId):
        self.primary_card = get_object_or_404(Card, first_tag_id=primary_card_firstTagId)
        return primary_card_firstTagId

    def validate(self, attrs):
        request = self.context.get('request')
        self.place: Place = request.place

        #TODO: Vérifier l'abonnement
        transaction_dict = {
            "ip": get_request_ip(request),
            "checkout_stripe": None,
            "sender": self.card.get_wallet(),
            "receiver": request.place.wallet,
            "asset": attrs.get('asset'),
            "amount": 0,
            "action": Transaction.BADGE,
            "metadata": self.initial_data,
            "primary_card": self.primary_card,
            "card": self.card,
            "subscription_start_datetime": None
        }
        transaction = Transaction.objects.create(**transaction_dict)
        self.transaction = transaction
        return attrs


class TransactionW2W(serializers.Serializer):
    amount = serializers.IntegerField()
    sender = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all())
    receiver = serializers.PrimaryKeyRelatedField(queryset=Wallet.objects.all())
    asset = serializers.PrimaryKeyRelatedField(queryset=Asset.objects.all())
    subscription_start_datetime = serializers.DateTimeField(required=False)
    action = serializers.ChoiceField(choices=Transaction.TYPE_ACTION, required=False, allow_null=True)

    first_token_uuid = serializers.UUIDField(required=False, allow_null=True)

    comment = serializers.CharField(required=False, allow_null=True)
    metadata = serializers.JSONField(required=False, allow_null=True)
    checkout_stripe = serializers.PrimaryKeyRelatedField(queryset=CheckoutStripe.objects.all(),
                                                         required=False, allow_null=True)

    primary_card_uuid = serializers.PrimaryKeyRelatedField(queryset=Card.objects.all(), required=False)
    primary_card_fisrtTagId = serializers.SlugRelatedField(
        queryset=Card.objects.all(),
        required=False, slug_field='first_tag_id')

    user_card_uuid = serializers.PrimaryKeyRelatedField(queryset=Card.objects.all(), required=False)
    user_card_firstTagId = serializers.SlugRelatedField(
        queryset=Card.objects.all(),
        required=False, slug_field='first_tag_id')

    def validate_amount(self, value):
        # Positive amount only
        if value <= 0:
            raise serializers.ValidationError("Amount must be positive")
        return value

    def validate_primary_card(self, value):
        # TODO; Check carte primaire et lieux
        return value

    def get_action(self, attrs):
        # Quel type de transaction ?
        action = None

        if (attrs.get('action') == Transaction.REFILL
                and self.checkout_stripe
                and self.sender.is_primary()
                and self.asset.is_stripe_primary()
        ):
            # C'est une recharge stripe
            return Transaction.REFILL

        # Un lieu est le sender, trois cas possibles : Adhésion / Badge / Recharge locale
        if self.place.wallet == self.sender:
            # adhésion / abonnement
            if self.asset.category == Asset.SUBSCRIPTION:
                return Transaction.SUBSCRIBE
            # Badgeuse
            if self.asset.category == Asset.BADGE:
                return Transaction.BADGE

            # ex methode, on ne fait plus qu'une seule requete maintenant.
            if self.sender == self.receiver:
                if self.asset.wallet_origin == self.place.wallet:
                    raise serializers.ValidationError('no longuer implemented for REFILL. Send user wallet instead')
                raise serializers.ValidationError("Unauthorized wallet_origin")

            # C'est une recharge locale, on a besoin de deux cartes
            if not self.primary_card or not self.user_card:
                raise serializers.ValidationError("Primary card and user card are required for refill transaction")
            return Transaction.REFILL

        elif self.place.wallet == self.receiver:
            if not self.primary_card:
                raise serializers.ValidationError("Primary card is required for sale transaction")
            if self.primary_card not in self.place.primary_cards.all():
                raise serializers.ValidationError("Primary card must be in place primary cards")
            if not self.user_card:
                raise serializers.ValidationError("User card is required for sale transaction")
            # Si le lieu du wallet est dans la délégation d'autorité du wallet de la carte
            if not self.receiver in self.user_card.get_authority_delegation():
                # Place must be in card user wallet authority delegation
                logger.warning(f"{timezone.localtime()} WARNING sender not in receiver authority delegation")
                raise serializers.ValidationError("Unauthorized")
            if self.asset not in self.place.accepted_assets():
                raise serializers.ValidationError("Asset not accepted")
            # Toute validation passée, c'est une vente
            return Transaction.SALE


        elif attrs.get('action') == Transaction.FUSION:
            # Liaison entre une carte avec wallet ephemere et un wallet user -> Fusion !
            # Le sender est le wallet ephemere d'une carte sans user
            # Le receiver est le wallet user d'un user déja existant
            # mais dont le wallet est différent du wallet_ephemere de la carte
            # C'est une fusion de deux wallet en faveur de celui de l'user : le receiver
            sender: Wallet = attrs.get('sender')
            receiver: Wallet = attrs.get('receiver')
            if (not getattr(sender, 'user', None)
                    and sender.card_ephemere
                    and receiver.user):

                # Uniquement avec une clé api de place pour le moment.
                # Pour que l'user puisse le faire en autonomie -> auth forte (tel, double auth, etc ...)
                if sender.card_ephemere.origin.place == self.place:
                    return Transaction.FUSION

        raise serializers.ValidationError("No action authorized")

    def validate(self, attrs):
        # Récupération de la place grâce à la permission HasKeyAndCashlessSignature
        request = self.context.get('request')
        # get variable
        self.sender: Wallet = attrs.get('sender')
        self.receiver: Wallet = attrs.get('receiver')
        self.asset: Asset = attrs.get('asset')
        self.amount: int = attrs.get('amount')
        self.comment: str = attrs.get('comment')
        self.metadata: str = attrs.get('metadata')
        self.checkout_stripe: CheckoutStripe = attrs.get('checkout_stripe', None)
        # Subscription :
        self.subscription_start_datetime = attrs.get('subscription_start_datetime')

        # Avons nous une carte user et/ou une carte primaire LaBoutik ?
        self.primary_card: Card = attrs.get('primary_card_uuid') or attrs.get('primary_card_fisrtTagId')
        self.user_card: Card = attrs.get('user_card_uuid') or attrs.get('user_card_firstTagId')

        self.place: Place = getattr(request, 'place', None)

        if not self.place:
            # C'est probablement une recharge stripe.
            # Le serializer est appellé par le webhook post paiement, il n'y a pas de place.
            if (attrs.get('action') == Transaction.REFILL
                    and self.checkout_stripe
                    and self.sender.is_primary()
                    and self.asset.is_stripe_primary()):
                # Si c'est une recharge depuis Stripe,
                # on met la place de l'origine de la carte
                self.place: Place = self.user_card.origin.place

            else:
                logger.error(f"{timezone.localtime()} ERROR NewTransactionWallet2WalletValidator : place not found")
                raise serializers.ValidationError("Place not found")

        action = self.get_action(attrs)
        if not action:
            # Si aucune des conditions d'action n'est remplie, c'est une erreur
            logger.error(
                f"{timezone.localtime()} ERROR ZERO ACTION FOUND - {request}")
            raise serializers.ValidationError("Unauthorized")

        # Check if sender or receiver are a place
        if (not self.place.wallet == self.sender
                and not self.place.wallet == self.receiver
                and not self.asset.is_stripe_primary()
                and not action == Transaction.FUSION):
            # Place must be sender or receiver
            logger.error(f"{timezone.localtime()} ERROR sender nor receiver are Unauthorized - {request}")
            raise serializers.ValidationError("Unauthorized")

        # get sender token
        try:
            token_sender = Token.objects.get(wallet=self.sender, asset=self.asset)
            # Check if sender has enough value
            if token_sender.value < self.amount and action in [Transaction.SALE, Transaction.TRANSFER]:
                logger.error(f"\n{timezone.localtime()} ERROR sender not enough value - {request}\n")
                # import ipdb; ipdb.set_trace()
                raise serializers.ValidationError("Not enough token on sender wallet")
        except Token.DoesNotExist:
            raise serializers.ValidationError("Sender token does not exist")

        # get or create receiver token
        try:
            self.token_receiver = Token.objects.get(wallet=self.receiver, asset=self.asset)
        except Token.DoesNotExist:
            logger.info(
                f"{timezone.localtime()} INFO NewTransactionWallet2WalletValidator : receiver token does not exist")
            self.token_receiver = Token.objects.create(wallet=self.receiver, asset=self.asset, value=0)

        ### ALL CHECK OK ###

        # Si c'est un refill, on génère la monnaie avant :
        if action == Transaction.REFILL:

            crea_transac_dict = {
                "ip": get_request_ip(request),
                "sender": self.sender,
                "receiver": self.sender,
                "asset": self.asset,
                "comment": self.comment,
                "metadata": self.metadata,
                "checkout_stripe": self.checkout_stripe,
                "amount": self.amount,
                "action": Transaction.CREATION,
                "primary_card": self.primary_card,
                "card": self.user_card,
            }
            crea_transaction = Transaction.objects.create(**crea_transac_dict)

            if not crea_transaction.verify_hash():
                logger.error(
                    f"{timezone.localtime()} ERROR NewTransactionWallet2WalletValidator : transaction hash is not valid on CREATION")
                raise serializers.ValidationError("Transaction hash is not valid")

        transaction_dict = {
            "ip": get_request_ip(request),
            "sender": self.sender,
            "receiver": self.receiver,
            "asset": self.asset,
            "comment": self.comment,
            "metadata": self.metadata,
            "checkout_stripe": self.checkout_stripe,
            "amount": self.amount,
            "action": action,
            "primary_card": self.primary_card,
            "card": self.user_card,
            "subscription_start_datetime": self.subscription_start_datetime
        }
        transaction = Transaction.objects.create(**transaction_dict)

        if not transaction.verify_hash():
            logger.error(
                f"{timezone.localtime()} ERROR NewTransactionWallet2WalletValidator : transaction hash is not valid")
            raise serializers.ValidationError("Transaction hash is not valid")

        self.transaction = transaction
        return attrs


class TransactionSerializer(serializers.ModelSerializer):
    card = CardSerializer(many=False)

    class Meta:
        model = Transaction
        fields = (
            "uuid",
            "action",
            "hash",
            "datetime",
            "subscription_first_datetime",
            "subscription_start_datetime",
            "subscription_type",
            "last_check",
            "sender",
            "receiver",
            "asset",
            "amount",
            "comment",
            "metadata",
            "card",
            "primary_card",
            "previous_transaction",
            "comment",
            "verify_hash",
        )


class FederationSerializer(serializers.ModelSerializer):
    places = PlaceSerializer(many=True)
    assets = AssetSerializer(many=True)

    class Meta:
        model = Federation
        fields = (
            'uuid',
            'name',
            'places',
            'assets',
            'description',
        )
